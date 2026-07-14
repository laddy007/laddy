---
type: feature
---

# task-flags — typované, event-sourced flagy na tasku v durable iteration-logu

## Authoritative design

> **Architektura loopu (závazná):**
> `docs/development/superpowers/specs/2026-07-05-agent-dev-loop-design.md`
> (zejména artifact-first stav §9 — append-only `iteration-log.jsonl`).

Rozhodnutí Directora (2026-07-11, závazná pro tento spec):

1. **Publikum = lokální.** Flagy čte Director v handbacku a přes CLI na
   svém stroji. Žádný externí tracker (GitHub issue), žádná okamžitá
   notifikace, žádné opuštění stroje.
2. **Úložiště = ten samý durable append-only log** (`iteration-log.jsonl`),
   ne nové úložiště. Durabilitu (přežití pádu — OOM, výpadek SSH) dává
   samotný okamžitý append na disk; flag zapsaný před pádem je bezpečně
   na disku.
3. **Flag = posloupnost událostí, ne mutovatelný záznam.** Stav flagu se
   neukládá — **derivuje se** z událostí, přesně jako `_derive_status`
   odvozuje stav tasku. Jeden fakt, jeden domov.
4. **Flagy dědí git-zacházení artefaktů** — nemají vlastní commit/scratch
   politiku. V automatickém loopu jedou v existujících per-fázových
   commitech na izolované větvi `agent/<task>` a jsou mimo `code_sha`
   (stejně jako verdikty/handback dnes). Ručně přes CLI se jen zapíšou na
   disk, necommitne je nic, dokud to Director neudělá.

## Goal

Během práce na tasku vznikají věci, které mají doputovat k Directorovi:
odchylka od plánu ke schválení, vědomě odložený dluh, blocker, otázka,
drobný nález. Dnes žijí jako **volný text** (ad-hoc ledger, próza) a na
konci se musí ručně dolovat a sumarizovat — křehké, ztratitelné při pádu,
nestrojově čitelné.

Cíl: dát tasku **typovaný, crash-safe kanál pro flagy** postavený na
existujícím durable logu. Agent (nebo Director přes CLI) vyvěsí flag
v momentě rozhodnutí → hned na disku → na konci se flagy **sesbírají**
z logu do handbacku a jsou vypsatelné přes `--phase flags`. Ruční próza
v ledgeru se stává zbytečnou (dá se generovat z logu).

## Non-goals

- **Žádný externí tracker / GitHub issue / okamžitá ntfy notifikace** na
  flag — publikum je lokální (Director + handback + CLI).
- **Žádná editace/komentování/přiřazování** flagu. Flag má jen dvě
  přechodové události: vznik a vyřešení.
- **Žádná auto-emise flagů z loop terminálů** (path-guard, quota-timeout,
  escalated-deadlock). Ty dnes mají vlastní `terminal` event + ntfy a
  **nemění se**. Napojení terminálů na flag kanál je budoucí fáze 2.
- **Žádné nové úložiště** (žádný `flags.jsonl`, žádná DB) — flag je řádek
  ve stávajícím `iteration-log.jsonl`.
- Žádná změna sémantiky existujících log událostí, `_PHASE_ACTIONS`,
  `code_sha`, merge/push politiky.

## Scope

### 1. Datový model — dvě append-only události (`iteration-log.jsonl`)

Do stávajícího logu přibydou **dva typy akce** (nic se nepřepisuje):

Vznik flagu (`action="flag"`):
```json
{"ts": "...", "action": "flag", "id": "<task>#N", "kind": "deviation",
 "summary": "zpřísnil regex proti spec AC2",
 "detail": "volitelný delší popis", "needs_director": true, "round": 2}
```
Vyřešení flagu (`action="flag-resolved"`):
```json
{"ts": "...", "action": "flag-resolved", "id": "<task>#N",
 "resolution": "resolved", "note": "Director: schváleno"}
```

- `kind` ∈ `deviation | debt | blocker | question | note` (uzavřená
  množina — modulová konstanta `FLAG_KINDS`).
- `needs_director: bool` — má flag čekat na rozhodnutí Directora? (default
  `false`; `note` typicky `info`).
- `detail`, `round` jsou volitelné.
- `resolution` ∈ `resolved | dismissed` (uzavřená množina
  `FLAG_RESOLUTIONS`; `resolved` = vyřízeno/schváleno/zodpovězeno,
  `dismissed` = zamítnuto/won't-do/šum). Rozhodovací nuanci (approved vs
  rejected) nese volný `note`. `note` je volitelný.
- **`id` = `"<task>#N"`**, kde `N` = 1 + počet dosavadních `flag`
  událostí (vzniků) v logu tasku. **Deterministické** (žádný random/uuid),
  resume-safe (append-only log je jediný zdroj). Přiděluje se při vzniku
  z aktuálního obsahu logu.

### 2. Modul `orchestrator/flags.py` (nový)

Drží veškerou flag logiku (držíme `artifacts.py` štíhlé — CLAUDE.md
„files don't grow, they split"). Obsahuje:

- Konstanty `FLAG_KINDS`, `FLAG_RESOLUTIONS`.
- `FLAG_ACTIONS = ("flag", "flag-resolved")` — pro použití v derivaci
  stavů (§5) jako denylist/rozpoznání ne-progress akcí.
- Frozen dataclass `Flag`: `id, kind, summary, detail, needs_director,
  round, raised_ts, status, resolution, note, resolved_ts`; `status` ∈
  `open | resolved | dismissed`.
- **Čistá funkce** `derive_flags(entries: list[dict]) -> list[Flag]`:
  fold logu na aktuální flagy. `flag` událost otevře flag (status
  `open`); odpovídající `flag-resolved` nastaví `status` z `resolution`
  (`resolved`/`dismissed`), `note`, `resolved_ts`. Pořadí = pořadí
  vzniku. `flag-resolved` na neznámé/již vyřešené `id` se **ignoruje**
  (defenzivně — nelze vyřešit, co nevzniklo). Plně unit-testovatelné,
  bez I/O.
- Tenké write helpery nad `TaskArtifacts.append_log` (existující
  crash-safe append): `raise_flag(art, kind, summary, *, detail, round,
  needs_director) -> str` (spočítá `id` z `art.read_log()`, zapíše
  `flag` událost, vrátí `id`) a `resolve_flag(art, flag_id, *,
  resolution, note) -> bool` (ověří, že `id` je aktuálně `open` přes
  `derive_flags`, jinak vrátí `False` a **nic nezapíše**; jinak zapíše
  `flag-resolved` a vrátí `True`). Čas jde přes `TaskArtifacts._now`
  (už injektovatelné) — žádné přímé `datetime.now`.

### 3. CLI — vyvěšení a výpis (`run.py`)

Nové fáze `orchestrator.run` (do `choices` v argparse). Umístění
artefaktového logu tasku se řeší **stejnou cestou jako `--phase status`**
(TaskArtifacts nad node-lokálním work_root / worktree) — žádná nová
lokace, žádné nové rozlišování.

- `--phase flag <task>` — dva vzájemně výlučné režimy (kombinace = chyba
  argparse):
  - **vznik**: `--kind <kind> --summary <text> [--detail <text>]
    [--needs-director] [--round N]` → zavolá `raise_flag`, vypíše
    přidělené `id`. `--kind` validuje argparse (`choices=FLAG_KINDS`);
    prázdný/chybějící `--summary` = chyba (exit 2).
  - **vyřešení**: `--resolve <id> [--resolution resolved|dismissed]
    [--note <text>]` → zavolá `resolve_flag`. `--resolution` default
    `resolved`. Neexistující/již vyřešené `id` → `resolve_flag` vrátí
    `False` → vypiš chybu, **exit nenulový** (návrh: 3), nic se
    nezapíše.
- `--phase flags [<task> ...]` — reporter (sourozenec `status`): pro
  zadané tasky (nebo všechny `specs/*.md`, pokud žádný) načti log,
  `derive_flags`, vypiš **jen OTEVŘENÉ** flagy seskupené po tasku,
  `needs_director` první, s krátkým souhrnným počtem (např.
  `3 open (1 needs-director)`). Vyřešené/zamítnuté se nevypisují.
  Prázdno → čistá hláška „no open flags". **Exit 0 vždy** (čistý
  reporter; signalizace přes exit kód je out of scope).

### 4. Vyplavání do handbacku (`handoff.py`)

- `build_handback` a `build_summary`/`write_human_summary` dostanou
  sekci **`⚑ Flags`**: vypíše otevřené flagy z `derive_flags(entries)`,
  `needs_director` první, formát `- [kind] summary (id)` + `note`/`detail`
  na dalším řádku, je-li. **Když nejsou otevřené flagy, sekce se
  vynechá** (žádný prázdný nadpis). Odvození flagů z týchž `entries`,
  které funkce už dnes čte — žádné nové načítání souborů.

### 5. Derivace stavů se flagy nesmí rozbít (`run.py` — `_derive_status`)

`flag`/`flag-resolved` událost **nesmí** převrátit task na `in-progress`
(jinak by vyvěšení flagu na `ready` tasku falešně označilo task za
rozdělaný — přesně kontaminace, jako dřív dělalo `clarify`).

- `_derive_status` in-progress kontrola se převede z **denylistu na
  allowlist**: task je `in-progress`, právě když log obsahuje aspoň jednu
  **pozitivní progress akci** (množina odvozená z `_PHASE_ACTIONS`), ne
  „log je neprázdný minus výjimky". Tím `clarify` **i** `flag`/
  `flag-resolved` (a jakákoli budoucí ne-progress akce) korektně
  nespouští `in-progress`. **Tímto se rovněž splácí dřív přijatý dluh**
  (denylist→allowlist z quota-resume-queue Tasku 7).
- `flag`/`flag-resolved` **nejsou** v `_PHASE_ACTIONS` → neinkrementují
  kola ani nevstupují do nonconvergence fingerprintů. `code_sha` je jako
  artefaktové řádky ignoruje (žádná změna `code_sha` logiky nutná —
  ověřit).

### 6. Unit testy (`tests/agent_orchestrator/`, běží v normální suite)

- `test_flags.py` (nový):
  - `derive_flags` tabulkově: samotný vznik → `open`; vznik+resolved →
    `resolved` s `note`; vznik+dismissed → `dismissed`; `flag-resolved`
    na neznámé `id` → ignorováno; dvojí resolve → druhý ignorován;
    pořadí zachováno; `needs_director` zachováno.
  - `raise_flag` přidělí `#1, #2, …` dle počtu vzniků; zapíše přesně
    jeden řádek; `read_log` po zápisu obsahuje předchozí řádky beze
    změny (append-only).
  - `resolve_flag`: open → zapíše `flag-resolved`, vrátí `True`;
    neznámé/již vyřešené `id` → `False`, **nic nezapsáno**.
- `test_run_cli.py` (rozšíření): `flag` vznik vypíše `id` a zapíše
  událost; špatný `--kind`/prázdný `--summary` → argparse/exit 2;
  `--resolve` neznámého `id` → exit 3, nic nezapsáno; vznik a resolve
  vzájemně výlučné; `flags` vypíše jen otevřené, needs-director první,
  se souhrnným počtem; prázdno → „no open flags", exit 0.
- `test_handoff.py` (rozšíření): handback i human-summary obsahují sekci
  `⚑ Flags` s otevřenými flagy (needs-director první); bez otevřených
  flagů sekce chybí.
- Derivace stavů (rozšíření stávajícího testu `_derive_status`): task
  jen s `flag` událostí (bez progress akce) zůstává `ready`, **ne**
  `in-progress`; task s progress akcí je `in-progress` i když nese flagy.
- Žádný test reálně nespí ani nevolá reálné LLM; čas přes injektovaný
  `now` (stávající vzor `TaskArtifacts(now=…)`).

## Constraints

- **Výjimka z role pravidla (explicitní):** tento task SMÍ měnit
  `.laddy/orchestrator/**` — Director tuto výjimku uděluje tímto specem.
  Pravidlo „never touch `.laddy/orchestrator/`" v `.laddy/roles/
  developer.md` pro tento task na tuto cestu neplatí. `.laddy/roles/*`,
  `.laddy/scripts/*`, `.laddy/docker/*`, `.laddy/security/*` a produkční
  kód zůstávají nedotčené.
- Merge větve rozhodne Director ručně — `orchestrator/*` je na
  `SENSITIVE_GLOBS`, auto-merge je vyloučen by design. Nic v tomto tasku
  se to nesmí pokusit změnit.
- Veškerá logika v Pythonu; **žádné nové bash skripty, žádný démon, žádná
  nová produkční dependency.**
- Artifact-first / append-only: flag je řádek v `iteration-log.jsonl`,
  nikdy se nepřepisuje; stav flagu se výhradně **derivuje**.
- Čas výhradně přes injektované `now`/`clock` (testovatelnost);
  `datetime.now(tz=UTC)` jen v produkčním default wiringu. **Žádný
  random/uuid** — `id` je deterministické z počtu událostí.
- Kód/commity/docstringy anglicky; LF; ASCII-safe (znak `⚑` je jen
  v renderovaném markdownu handbacku/summary, ne v kódu/logu — v logu
  jsou pole čistě ASCII klíče).
- Green: `ruff check .`, `basedpyright` pro dotčený scope,
  `pytest -n auto -q`.
- TDD pro veškerou novou logiku (`derive_flags`, write helpery, CLI,
  handback rendering, derivace stavů).
- Nemergovat do `main`, nepushovat mimo `agent/<task>` (Tier 3 — Director).

## Acceptance criteria

1. `derive_flags` (čistá funkce) foldne vznik+resolve události na `Flag`
   objekty se správným `status` (`open`/`resolved`/`dismissed`), `note`,
   `resolved_ts` a pořadím; `flag-resolved` na neznámé/již vyřešené `id`
   je ignorováno; `needs_director` zachováno. Tabulkový test.
2. `raise_flag` přidělí `id` `<task>#1`, `#2`, … dle počtu dosavadních
   vzniků, zapíše přesně jeden append a nepřepíše předchozí řádky
   (append-only ověřeno). `resolve_flag` otevřeného flagu zapíše
   `flag-resolved` a vrátí `True`; na neznámé/již vyřešené `id` vrátí
   `False` a **nic nezapíše**. Pokryto testy.
3. `--phase flag <task> --kind <kind> --summary <text>` zapíše `flag`
   událost a vypíše přidělené `id`; neplatný `--kind` nebo prázdný
   `--summary` skončí chybou argparse/exit 2; režimy vznik a `--resolve`
   jsou vzájemně výlučné; `--resolve` neexistujícího `id` skončí exit 3
   bez zápisu. Pokryto testy.
4. `--phase flags` vypíše jen **otevřené** flagy seskupené po tasku,
   `needs_director` první, se souhrnným počtem; vyřešené/zamítnuté
   nevypisuje; prázdný stav vypíše „no open flags" a skončí exit 0.
   Log se čte stejnou cestou jako `--phase status`. Pokryto testy.
5. `build_handback` i human-summary obsahují sekci `⚑ Flags` s otevřenými
   flagy (needs-director první); při nula otevřených flagech sekce chybí.
   Pokryto testy.
6. **Derivace stavů odolná vůči flagům:** task nesoucí jen `flag`/
   `flag-resolved` události (žádná progress akce) se odvodí jako `ready`,
   ne `in-progress`; task s progress akcí zůstává `in-progress` i s flagy.
   `_derive_status` in-progress kontrola je allowlist pozitivních
   progress akcí (splácí dřívější denylist dluh). `flag`/`flag-resolved`
   nejsou v `_PHASE_ACTIONS` (kola/fingerprints/`code_sha` nedotčeny).
   Pokryto testy.
7. `pytest -n auto -q` zelené, `ruff check .` clean, `basedpyright` clean
   pro dotčený scope. Diff nesahá mimo `.laddy/orchestrator/**`,
   `tests/**`, `.laddy/tasks/<task>/**` (artefakty) a
   `.laddy/specs/task-flags.md` (clarify append) — grep-ověřitelné.

## Notes for the reviewer

- **Výjimka na `.laddy/orchestrator/**` je udělená specem** (viz
  Constraints) — změny tam NEJSOU nález. Změny v `.laddy/roles/*`,
  `.laddy/scripts/*`, `.laddy/docker/*`, `.laddy/security/*` nález JSOU
  (CHANGES_REQUESTED).
- Ověř, že flag je **jen** dvojice append-only událostí v existujícím
  logu — žádné nové úložiště, žádný `flags.jsonl`, žádná mutace řádků.
- Ověř, že `id` je **deterministické** (z počtu událostí), bez
  random/uuid, a resume-safe.
- Ověř, že `flag`/`flag-resolved` **nejsou** v `_PHASE_ACTIONS` a že task
  s pouhými flagy se odvodí jako `ready` (jinak vyvěšení flagu falešně
  označí task za rozdělaný — regrese ekvivalentní clarify-only bugu).
- Ověř, že existující loop události (`quota_exhausted`, `terminal`,
  fázové akce) a jejich ntfy chování zůstaly **nezměněné** — flagy jsou
  aditivní, ne refactor.
- Ověř, že testy nikde reálně nespí a nevolají reálné LLM; čas jde přes
  injektovaný `now`.
- Reject, pokud se objevil nový bash skript, démon, nová produkční
  dependency, externí-tracker integrace, nebo pokus o oslabení merge
  politiky.
