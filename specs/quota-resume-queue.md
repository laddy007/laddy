---
type: feature
---

# quota-resume-queue — kvótově orientovaná fronta tasků + auto-resume po resetu předplatného okna

## Authoritative design

> **Inspirační doc (kontext a zdůvodnění priorit):**
> `docs/development/superpowers/specs/2026-07-11-agent-dev-loop-oss-inspiration.md`
> — sekce „Quota-window utilization" a „Subscription economics".
> **Architektura loopu (závazná):**
> `docs/development/superpowers/specs/2026-07-05-agent-dev-loop-design.md`
> (zejména artifact-first stav §9, komponenty §11).

Rozhodnutí Directora (2026-07-11, závazná pro tento spec):

1. Rozsah = auto-resume běžícího tasku **plus** fronta speců.
2. Čekání na reset = **in-process sleep** v detached loopu (nohup proces
   už přežívá SSH drop). Přežití rebootu VPS je out of scope (samostatný
   budoucí task „reconcile-stale").
3. Fronta přijímá **jen tasky s hotovým clarify gate** (nebo explicitní
   `--skip-clarify`) — task spuštěný z fronty bez obsluhy se nemá koho ptát.

## Goal

Agenti běží na měsíčním předplatném (Claude Max, Codex) — vzácným zdrojem
není dolar, ale **kvótové okno** (5h/týdenní limity). Dnes rate-limit
uprostřed smyčky vypadá jako obyčejný `error`: kolo propadne, loop dojede
do capu nebo skončí terminálně, a zbytek okna i celé noční okno propadnou.

Cíl: orchestrátor rozpozná vyčerpání kvóty jako **zotavitelný stav**
(`QUOTA_EXHAUSTED`), počká na reset okna a naváže tam, kde přestal.
Po dokončení tasku si vezme další připravený spec z fronty. Okna —
včetně nočních — se vytěžují naplno, bez zásahu Directora.

## Non-goals

- Přežití rebootu VPS / kill -9 čekajícího procesu (budoucí
  „reconcile-stale" task; artifact-first stav už dnes umožňuje ruční
  re-kickoff).
- Paralelní běh více tasků najednou — fronta je striktně sekvenční,
  jeden aktivní loop (kvóta je sdílená, paralelismus by ji jen dřív vybil).
- Žádné USD/token počítání ani predikce spotřeby — jen reakce na
  skutečný rate-limit signál z CLI.
- Žádné změny v `merge-verified.sh` / lokální merge autoritě.
- Žádné změny v `.laddy/scripts/*` (bash zůstává tenký; enqueue i queue
  runner jsou nové fáze `orchestrator.run`).
- Žádný nový démon/systemd/cron.

## Scope

### 1. Klasifikace rate-limitu (`agents.py`)

- `AgentResult` dostane nový `exit_reason: "quota"` (vedle `ok`/`error`)
  a nové pole `quota_reset_at: datetime | None` (UTC).
- `ClaudeRunner`: klasifikuj z JSON payloadu i z plain-text výstupu.
  Vzory drž jako modulové konstanty (jedno místo, snadná aktualizace):
  case-insensitive substring/regex na `usage limit`, `rate limit`,
  `limit will reset`, `resets at`, `hit your limit` v `result`/`stderr`
  při `is_error`/nenulovém rc. Pokus se vyparsovat čas resetu z hlášky
  (Claude CLI typicky uvádí čas resetu; parsuj defenzivně — absolutní čas
  i relativní tvary typu „resets in 2 hours").
- `CodexRunner`: totéž nad stdout/stderr (vzory `rate limit`, `quota`,
  `usage limit`, `429`, `too many requests`). Codex reset time typicky
  nehlásí → `quota_reset_at=None` je validní výsledek.
- Nejistá klasifikace = `error` (konzervativně): quota stav smí vzniknout
  jen z explicitního vzoru, jinak platí dosavadní chování.

### 2. Čekání a resume (`loop.py` + nový `orchestrator/quota.py`)

- Nový modul `quota.py`: čistá funkce
  `wait_plan(reset_at: datetime | None, attempt: int, now: datetime) -> timedelta`
  — s known reset time čekej do `reset_at` + malý buffer (default 120 s);
  bez něj exponenciální backoff schedule (default 15 min → 30 min → 60 min,
  pak po 60 min). Kumulativní strop `QUOTA_MAX_WAIT_HOURS` (default 30 —
  pokryje víkendový/weekly reset) → po překročení terminální stav
  `QUOTA_TIMEOUT` + human-summary.
- V orchestrátoru: když libovolný agent run (developer, rw1, rw2, senior,
  clarify ne — ta je interaktivní) vrátí `exit_reason == "quota"`:
  - zapiš append-only event `quota_exhausted` do `iteration-log.jsonl`
    (role, attempt, reset_at, plánované čekání),
  - pošli **jednu** ntfy notifikaci (`[task] quota exhausted, resuming
    ~HH:MM UTC`), při opakovaném čekání v témže kole už ne,
  - spi podle `wait_plan` (injektovaná `sleep_fn`/`clock` — testy nesmí
    reálně spát), po probuzení **zopakuj tentýž krok téhož kola**,
  - quota čekání **neinkrementuje** loop counter (`MAX_LOOPS` počítá
    konvergenční kola, ne čekání) a nezapočítává se do
    nonconvergence fingerprints,
  - po úspěšném navázání zapiš event `quota_resumed`.
- Fallback: pokud po probuzení první run znovu vrátí `quota`, pokračuj
  podle backoff schedule (attempt += 1) až do stropu.

### 3. Fronta speců (nový `orchestrator/queue.py` + fáze v `run.py`)

- Fronta je node-lokální runtime stav pod `AGENT_WORK_ROOT` (např.
  `<work_root>/queue/`): jeden soubor na položku
  `NNNN-<task>.json` (pořadí = FIFO dle prefixu; obsah: task_id,
  enqueued_at, skip_clarify flag). Nic z fronty se necommituje do repa.
- **Done-marker ve specu**: `TaskSpec` dostane `is_done` property —
  front matter `status: done` značí dokončený spec. Historické specy
  jsou orazítkované předem (commit `2552107f`, mimo tento task).
  Razítkování po merge zůstává ruční (Director/agent) — automatické
  stampování v `merge-verified.sh` je budoucí samostatný task.
- Nové fáze `orchestrator.run` — enqueue má **tři režimy výběru**
  (vzájemně výlučné; kombinace = chyba argparse):
  - `--phase enqueue <task> [<task2> ...]` — explicitní výčet. Pro
    každý task validuj: spec existuje + není draft (`_load_spec`)
    **a clarify gate už proběhl** (`has_clarify` nad artefakty tasku;
    s `--skip-clarify` lze vynechat — explicitní volba Directora,
    platí pro celé volání). Validace **all-or-nothing**: nejdřív
    zvaliduj všechny, teprve pak zařaď — při chybě kteréhokoli se
    nezařadí nic. Explicitní výčet smí zařadit i `status: done` spec
    (vědomé opakování je legitimní).
  - `--phase enqueue --pick` — interaktivní výběr: vypiš očíslované
    kandidáty a nech Directora vybrat (vstup přes injektované
    `deps.ask`, formát `1 3-5`; prázdný vstup = nic). Vybrané tasky
    pak projdou toutéž all-or-nothing validací jako explicitní výčet.
  - `--phase enqueue --all` — zařaď všechny kandidáty; task bez
    proběhlého clarify **přeskoč s varováním** (ne fail — `--all` je
    bezobslužný sběr, nemá padat kvůli jednomu nepřipravenému specu);
    `--skip-clarify` vypne i tento filtr.
  - **Kandidát** (pro `--pick`/`--all`) se určuje z odvozeného stavu
    (viz §4): `--all` bere jen `ready`; `--pick` nabízí `ready` +
    `in-progress` (rozdělané tasky, viditelně označené — výběr = vědomé
    navázání). Neparsovatelný spec při discovery nepadá — přeskočí se
    s varováním.
  - Spuštění `--phase queue` v době vyčerpaného okna je podporované
    z podstaty: první agent call prvního tasku vrátí quota signál a
    loop počká na reset (týž mechanismus jako čekání uprostřed běhu) —
    frontu lze tedy naplnit a odpálit kdykoli, rozjede se po otevření
    okna.
  - **Sémantika opakovaného zařazení** (plyne z artifact-first resume,
    nevyžaduje nový kód — ověřit testem, nedělat znovu):
    - done task zařazený explicitně = no-op (log končí `push → ok`,
      resume jde rovnou do `done`);
    - rozdělaný task naváže z artefaktů tam, kde skončil; spotřebovaná
      kola se dál počítají do `MAX_LOOPS`;
    - task po `CAP_REACHED`/`ESCALATED_DEADLOCK` se vrátí rovnou do
      téhož terminálu (bez práce) — nejdřív upravit spec dle handback.
  - `--phase queue`: single-flight runner — file lock
    (`<work_root>/queue/.lock`, `flock` nebo O_EXCL) zaručí max jeden
    aktivní loop na node. Smyčka: vezmi první položku → spusť pro ni
    ekvivalent `--phase loop` → po terminálním stavu položku odstraň
    (i při neúspěchu tasku — CAP_REACHED/ESCALATED nefronta znovu,
    Director dostane ntfy jako dnes) → další položka. Prázdná fronta =
    proces skončí (žádný polling démon).
  - `--phase queue-list`: vypiš frontu (pořadí, task, stáří) — pro
    `watch-vps.sh`/ruční kontrolu.
- Aktivní task, který čeká na kvótu, drží lock — fronta přirozeně čeká
  s ním.

### 4. Odvozené stavy tasků + per-task run lock

Stav tasku se **neukládá** (kromě záměru Directora ve front matter:
`draft-proposal` / `done` / nic) — runtime stav se **odvozuje** z toho,
co už existuje. Jeden fakt, jeden domov; žádný druhý zdroj pravdy.

- **Per-task run lock**: `--phase loop` drží po dobu běhu
  `<work_root>/locks/<task>.lock` (O_EXCL, pid, uvolnit ve finally).
  Druhá smyčka nad týmž taskem (z kickoffu i z fronty) se odmítne
  spustit (exit code 4). Fronta na lock-odmítnutí položku odstraní
  s varováním — dřívější pravidlo „task patří buď kickoffu, nebo
  frontě" je tím vynucené strukturálně, ne disciplínou.
- **Terminal marker v logu**: `_terminal_failure` nově appenduje
  `action="terminal", outcome=<stav>` do iteration logu (akce není
  v `_PHASE_ACTIONS`, resume derivaci neovlivní) — bez něj nejsou
  `CAP_REACHED`/`QUOTA_TIMEOUT` z artefaktů spolehlivě čitelné.
- **`--phase status`**: vypíše všechny `specs/*.md` s odvozeným stavem
  (node-lokální pohled — artefakty se čtou z lokálních worktree pod
  `<work_root>/wt/<task>`). Pořadí kontrol (dřívější vyhrává):
  `unparseable` → `draft` / `done` (front matter) → `running` (run
  lock) → `queued` (fronta) → `failed:<stav>` (poslední `terminal`
  entry v logu) → `pushed` (log obsahuje `push → ok`) → `in-progress`
  (log neprázdný) → `ready` (nic z výše uvedeného).
- Kandidáti `--pick`/`--all` se určují touto derivací (viz §3).

### 5. Konfigurace (`config.py` + `env.vps.example`)

Nové knoby (env, s defaulty; do `env.vps.example` s komentářem):
`QUOTA_RESET_BUFFER_SECONDS=120`, `QUOTA_BACKOFF_MINUTES=15,30,60`,
`QUOTA_MAX_WAIT_HOURS=30`.

### 6. Unit testy (`tests/`, běží v normální pytest suite)

- Klasifikace: tabulkový test vzorových Claude/Codex výstupů →
  `quota` vs `error` vs `ok`; parsování reset času (absolutní, relativní,
  chybějící); nejistý výstup → `error`.
- `wait_plan`: known reset → čekání do reset+buffer; unknown → backoff
  schedule; strop → `QUOTA_TIMEOUT`.
- Loop s fake runnerem: quota uprostřed kola → event → (fake) sleep →
  retry téhož kroku → úspěšné dokončení; loop counter neinkrementován;
  jedna ntfy notifikace na čekání.
- Fronta: FIFO pořadí; enqueue bez clarify odmítnut; enqueue draftu
  odmítnut; lock drží jediný runner; položka odstraněna po terminálním
  stavu včetně neúspěšného; prázdná fronta = čistý exit.
- Stavy + run lock: derivace všech stavů z §4 (tabulkový test nad
  syntetickými worktree/frontou/locky); druhý loop nad drženým run
  lockem se odmítne (exit 4); fronta na lock-odmítnutí odstraní
  položku s varováním; `--pick` nabízí ready + označené in-progress,
  `--all` bere jen ready.
- Žádný test reálně nespí a nevolá reálné LLM (injektované fakes —
  stávající vzor).

## Constraints

- **Výjimka z role pravidla (explicitní):** tento task SMÍ měnit
  `.laddy/orchestrator/**` a `.laddy/env.vps.example` — Director tuto
  výjimku uděluje tímto specem. Pravidlo „never touch" v
  `.laddy/roles/developer.md` pro tento task na uvedené cesty neplatí.
  `.laddy/roles/*`, `.laddy/scripts/*`, `.laddy/docker/*`,
  `.laddy/security/*` zůstávají nedotčené i zde.
- Merge větve rozhodne Director ručně — `orchestrator/*` je na
  `SENSITIVE_GLOBS`, auto-merge je vyloučen by design. Nic v tomto tasku
  se to nesmí pokusit změnit.
- Veškerá policy/stav/rozhodnutí v Pythonu; žádné nové bash skripty.
- Artifact-first: stav čekání je rekonstruovatelný z
  `iteration-log.jsonl` (eventy `quota_exhausted`/`quota_resumed`);
  session `-r` zůstává jen optimalizace.
- Čas a spánek výhradně přes injektované `clock`/`sleep_fn` (testovatelnost;
  `datetime.now(tz=UTC)` jen v produkčním default wiring).
- Minimální závislosti: žádná nová produkční dependency; parsování časů
  vlastní úzkou funkcí, ne novou knihovnou.
- Kód/commity/docstringy anglicky; LF; ASCII-safe.
- Green: `ruff check .`, `basedpyright` pro dotčený scope,
  `pytest -n auto -q`.
- TDD pro novou logiku (klasifikace, wait_plan, queue, loop integrace).
- Nemergovat do `main`, nepushovat mimo `agent/<task>` (Tier 3 — Director).

## Acceptance criteria

1. Fake runner vrátí quota-klasifikovaný výsledek uprostřed kola →
   loop zapíše `quota_exhausted` event, (fake) počká dle `wait_plan`,
   zopakuje tentýž krok a po `ok` výsledku normálně pokračuje až do
   terminálního stavu; loop counter se čekáním nezměnil. Pokryto testem.
2. Klasifikátor správně označí reprezentativní sadu reálných hlášek
   Claude CLI (s reset časem i bez) a Codex CLI; neznámá chyba zůstává
   `error`. Tabulkový test.
3. `wait_plan`: reset+buffer / backoff schedule / kumulativní strop →
   `QUOTA_TIMEOUT` s human-summary. Pokryto testy.
4. `--phase enqueue` odmítne task bez proběhlého clarify (a bez
   `--skip-clarify`), odmítne draft spec i neexistující spec; jinak
   zapíše FIFO položku. Víc tasků najednou: validace all-or-nothing —
   jeden nevalidní task znamená, že se nezařadí žádný. Pokryto testy.
4b. Discovery kandidátů jede přes derivaci stavů (§4): `--all` bere
   jen `ready`; `--pick` nabízí `ready` + označené `in-progress` a
   zařadí přesně tasky vybrané přes fake `ask` (formát `1 3-5`);
   nepřipravené (bez clarify) `--all` přeskočí s varováním, bez pádu.
   Režimy jsou vzájemně výlučné (argparse error). Pokryto testy.
4c. `--phase status` vypíše odvozený stav každého specu přesně dle
   pořadí kontrol v §4. Per-task run lock: druhý `--phase loop` nad
   týmž taskem skončí exit 4 bez spuštění smyčky; fronta na exit 4
   položku odstraní s varováním a pokračuje. `_terminal_failure`
   zapisuje `terminal` entry a resume derivace ji ignoruje. Pokryto
   testy.
5. `--phase queue` zpracuje frontu sekvenčně: po terminálním stavu
   (úspěch i neúspěch) položku odstraní a vezme další; druhý souběžný
   `--phase queue` na témže node se odmítne spustit (lock). Pokryto testy.
6. Quota čekání jednoho tasku blokuje frontu (žádný druhý task neběží
   souběžně). Pokryto testem.
7. Jedna ntfy notifikace na quota čekání (ne na každý backoff pokus);
   event log je append-only. Pokryto testy.
8. `pytest -n auto -q` zelené, `ruff check .` clean, `basedpyright`
   clean pro dotčený scope. Diff nesahá mimo `.laddy/orchestrator/**`,
   `.laddy/env.vps.example`, `tests/**`, `.laddy/tasks/<task>/**`
   (artefakty) a `.laddy/specs/quota-resume-queue.md` (clarify append)
   — grep-ověřitelné.

## Notes for the reviewer

- **Výjimka na orchestrátor je udělená specem** (viz Constraints) —
  změny v `.laddy/orchestrator/**` NEJSOU nález. Změny v
  `.laddy/roles/*`, `.laddy/scripts/*`, `.laddy/docker/*`,
  `.laddy/security/*` nález JSOU (CHANGES_REQUESTED).
- Ověř konzervativní klasifikaci: `quota` smí vzniknout jen z
  explicitního vzoru; default je `error`. Opačný směr (všechno je
  quota) by uměl zamaskovat skutečné chyby nekonečným čekáním.
- Ověř, že `MAX_LOOPS` a nonconvergence fingerprints quota čekání
  ignorují — jinak čekání „spotřebuje" konvergenční rozpočet tasku.
- Ověř single-flight lock: dva `--phase queue` procesy nesmí běžet
  souběžně (sdílená kvóta + kolize worktree).
- Ověř, že testy nikde reálně nespí (žádné `time.sleep` v test path)
  a nevolají reálné LLM.
- Ověř, že fronta žije pod `AGENT_WORK_ROOT` a nic z ní se necommituje.
- Reject, pokud se objevil nový bash skript, nový démon, nová produkční
  dependency, nebo pokus o oslabení merge policy.
