---
type: feature
roles: [developer, rw1, rw2]
risk: high
---
# loop-budget-per-gate — iteration cap per brána s resetem na pokrok

## Goal

Změnit význam `MAX_LOOPS` z **jednoho globálního čítače developer kol napříč
celým runem** na **per-brána budget, který se resetuje při pokroku**. Cílem je
původní záměr capu — „ať se to necyklí na stejné úrovni" — který dnešní
implementace neplní: se stále rostoucí paletou rolí (developer, rw1, rw2,
senior, authoritative gate) globální součet ztrácí smysl a jedna brána ukrádá
budget druhé.

Konkrétní bolest, kterou to řeší: v sekvenci

```
dev → fast_tests(fail) → dev → fast_tests(fail) → dev → fast_tests(pass)
    → rw1(changes_requested) → dev
```

dnes při `MAX_LOOPS=3` narazíme na `CAP_REACHED`, přestože **rw1 vrátil kód
jen jednou** — první dvě kola sežral test-fixing. Po změně měří každou bránu
vlastní budget: rw1 má plný počet pokusů nezávisle na tom, kolikrát se
předtím opravovaly testy.

## Root-cause context

Veškerá cap logika žije v jediné čisté funkci `derive_resume_point`
(`orchestrator/loop.py`), která přehraje append-only log a spočítá další
fázi. Dnes drží jeden čítač:

```python
rounds_used = 0
for entry in entries:
    if action == "developer":
        rounds_used += 1
...
if next_phase == "developer" and rounds_used >= max_loops:
    next_phase = "cap_reached"
```

Dvě klíčová pozorování, na kterých návrh stojí:

1. **Pole `round` je čistě kosmetické.** `derive_resume_point` si `round` z
   log entry nikdy nečte zpět — pokaždé ho přepočítá z akcí. `round` se jen
   *zapisuje* do log entries a commit messages jako label (`flags.py`,
   „Round N: …"). Čítač pro **cap** a čítač pro **label** jdou proto oddělit
   beze změny chování labelu.

2. **Jedno počítadlo principiálně nemůže splnit obojí naráz:**
   - Aby se dev⇄rw1 smyčka vůbec capovala, `fast_tests pass` **nesmí**
     resetovat čítač — jenže k rw1 se nedá dostat jinak než přes
     `fast_tests pass` (transition `("fast_tests","pass") → "rw1"`), takže
     reset na passu by rw1 smyčku učinil vůči capu imunní (čítač by
     oscioval 0→1→0→1 a nikdy nepřelezl 1).
   - Aby test-fixing nežral review budget, `fast_tests fail` **nesmí** čerpat
     review budget.
   - Jediné řešení, které splní obojí: `fast_tests` eventy do review čítače
     vůbec nevstupují → **oddělené čítače per brána.**

Zobecněno na všechny brány dostáváme jednotící princip: **každá brána
dostane `MAX_LOOPS` po sobě jdoucích selhání; průchod tou branou (pokrok)
budget obnoví.** „Pass/approve resetuje" platí uniformně, jen „pokrok"
znamená na každé bráně něco jiného.

## Scope

**In:**

- `derive_resume_point` (`orchestrator/loop.py`): nahradit jediný
  `rounds_used`-cap čtyřmi nezávislými per-brána budgety (viz Behaviour).
  `rounds_used` **zůstává** — už jen jako absolutní zdroj pro `round` label
  (monotónní přes celý run, beze změny).
- `ResumePoint`: přidat pole `cap_cause: str | None` (`"review" | "tests" |
  "authoritative" | "dev_error" | None`), aby handback/human-summary uměl
  říct, na které bráně to viselo, bez nového terminálu.
- `_finalize` / `write_human_summary` cesta: při `CAP_REACHED` vypsat
  `cap_cause` do human-summary (Director hned vidí příčinu).
- Unit testy nad `derive_resume_point` v `tests/test_loop_resume.py` (a kde
  je relevantní `tests/test_loop.py`).
- Dokumentace významu `MAX_LOOPS`: `SECURITY.md`, `specs/agent-dev-loop-slice1.md`
  (poznámka o změně sémantiky), `specs/quota-resume-queue.md` (odkaz na
  „loop counter").

**Out:**

- **Žádný nový terminál.** Cap zůstává `CAP_REACHED` pro všechny brány;
  příčina se nese v `cap_cause` (a v human-summary), ne v novém terminálovém
  stavu. (Samostatný `TESTS_STUCK` terminál je vědomě odložený follow-up —
  nesahat kvůli tomu na `terminals.py`, merge-check ani flags reporting.)
- **Žádná nová env proměnná.** Všechny čtyři budgety sdílejí hodnotu
  `MAX_LOOPS`; per-brána limity se nekonfigurují zvlášť.
- Žádná změna `round` labelu ani commit-message formátu (zůstává absolutní).
- Žádná změna backstopu `nonconvergence_detected` / senior eskalace / quota
  logiky — jen se ověří, že interakce sedí (viz Notes).
- Žádná změna transition tabulky (kam která fáze vede).

## Behaviour

`derive_resume_point` v jednom průchodu logem vede **čtyři nezávislé
budgety**, každý = „počet po sobě jdoucích selhání té brány od jejího
posledního průchodu". Increment i reset se řídí `(action, outcome)` právě
zpracovávaného log entry:

| Budget | +1 čerpá (`action`, `outcome`) | reset na 0 (`action`, `outcome`) |
|---|---|---|
| **review** | `rw1`/`changes_requested`, `rw1`/`malformed`, `rw2`/`nogo`, `rw2`/`malformed`, `senior`/`changes_requested` | `rw1`/`approved`, `rw2`/`go`, `senior`/`approved` |
| **tests** | `fast_tests`/`fail` | `fast_tests`/`pass` |
| **authoritative** | `authoritative`/`fail` | `authoritative`/`pass` |
| **dev_error** | `developer`/`<outcome != "ok">` | `developer`/`ok` |

Pravidla:

1. Reset a increment se aplikují **v pořadí, jak entries přicházejí v logu**
   (reset event předchází dalšímu developerovi, takže následující kolo
   startuje od čerstvého budgetu).
2. **`fast_tests pass` resetuje POUZE `tests`**, ne `review` — to je jádro
   fixu (viz Root-cause bod 2). Review budget resetuje výhradně review
   **approve** (posun o úroveň výš).
3. **Cap check** (nahrazuje dnešní `rounds_used >= max_loops`): když
   vypočtená `next_phase == "developer"`, tak

   ```
   if   review_used        >= max_loops: next_phase, cap_cause = "cap_reached", "review"
   elif tests_used         >= max_loops: next_phase, cap_cause = "cap_reached", "tests"
   elif authoritative_used >= max_loops: next_phase, cap_cause = "cap_reached", "authoritative"
   elif dev_error_used     >= max_loops: next_phase, cap_cause = "cap_reached", "dev_error"
   ```

   (Pořadí priorit při souběhu je dané tímto pořadím; v praxi je při cestě na
   developera nenulový typicky jen jeden budget.)
4. **`round` label beze změny**: `rounds_used` dál počítá *všechny*
   developer entries a `next_round` se odvozuje jako dnes
   (`rounds_used + 1` pro developer/cap_reached, jinak `max(rounds_used, 1)`).
   Cap už ale na `rounds_used` **nezávisí**.
5. `cap_cause` je `None` pro jakoukoli jinou `next_phase` než `cap_reached`.

Sémantika `MAX_LOOPS=N` po změně: **každá brána smí selhat N-krát v řadě**;
N-té selhání téže brány bez průchvbu vede na `CAP_REACHED`. Průchod branou
(green testy / rw approve / clean dev run) tuto bránu vynuluje. `MAX_LOOPS`
tedy neomezuje součet práce v runu, ale hloubku zacyklení na *jedné* bráně.

## Acceptance criteria

Testy jsou nad čistou `derive_resume_point` (žádné reálné LLM/git); log se
staví z fake entries jako ve stávajícím `tests/test_loop_resume.py`.

1. **Test-fixing nekrade review budget.** Log
   `dev·ft-fail·dev·ft-fail·dev·ft-pass·rw1-cr` při `max_loops=3` →
   `derive_resume_point(...).phase == "developer"` (ne `cap_reached`); v témž
   scénáři `cap_cause is None`.
2. **Review budget se capuje sám.** Log s `max_loops=3`, kde se opakuje
   `dev·ft-pass·rw1-cr` třikrát (tři `rw1 changes_requested`, mezi nimi vždy
   `fast_tests pass`) → další `developer` je `cap_reached` s
   `cap_cause == "review"`. (Ověřuje, že `fast_tests pass` NEresetuje review
   — regresní zámek na Root-cause bod 2.)
3. **Test budget se capuje sám.** `max_loops=3`, tři po sobě jdoucí
   `developer·fast_tests(fail)` bez jediného passu → `cap_reached` s
   `cap_cause == "tests"`. (Zachová chování dnešního
   `test_cap_reached_when_next_developer_round_exceeds_max`, jen s
   `cap_cause`.)
4. **Review approve resetuje review budget.** `max_loops=3`: dvě
   `rw1 changes_requested`, pak `rw1 approved` (s rw2 v kompozici → jde na
   `rw2`), pak `rw2 nogo` zpět na developera → review_used po approvu vynulován,
   takže rw1 smí opět dvakrát vrátit, než se capne. Vyjádřeno testem: po
   `rw1-approved` vede log s jedním následným `rw1 changes_requested` stále na
   `developer`, ne `cap_reached`.
5. **Authoritative gate má vlastní budget.** `max_loops=3`, tři po sobě jdoucí
   `authoritative fail` (bez passu) → `cap_reached`, `cap_cause ==
   "authoritative"`; jeden `authoritative pass` mezitím čítač vynuluje.
6. **Dev-error gate má vlastní budget (žádná regrese).** `max_loops=3`, tři po
   sobě jdoucí `developer` entry s `outcome != "ok"` → `cap_reached`,
   `cap_cause == "dev_error"`; `developer/ok` mezitím čítač vynuluje.
7. **`round` label zůstává absolutní a monotónní.** Ve scénáři z AC1
   (6+ developer/gate entries) `ResumePoint.round` roste monotónně s počtem
   developer kol (nereset­uje se s budgety) — ověřeno asserty na `round` v
   dostatečně dlouhém logu.
8. **Zpětná kompatibilita čistých dev⇄rw1/dev⇄testy scénářů.** Stávající cap
   testy v suite (mj. `test_cap_reached_when_next_developer_round_exceeds_max`,
   `test_cap_not_reached_when_last_round_still_in_review`) po úpravě procházejí
   — buď beze změny (chování identické), nebo jen s doplněným `cap_cause`
   assertem; žádný stávající test se neruší kvůli oslabení capu.
9. **Human-summary nese příčinu.** Při `CAP_REACHED` obsahuje
   `human-summary.md` řetězec identifikující `cap_cause` (např. „review" /
   „tests" / „authoritative" / „dev_error") — pokryto testem nad
   handback/summary cestou.
10. Suite green: `pytest -n auto -q` zelené, `ruff check .` clean,
    `basedpyright` clean pro dotčený scope. Terminál set (`terminals.py`)
    **beze změny** — grep-ověřitelné, že nepřibyl žádný nový terminálový stav.

## Notes for the reviewer

- **Ověř Root-cause bod 2 jako regresní zámek:** kdyby implementace resetovala
  review budget i na `fast_tests pass` (nebo počítala `fast_tests` do review
  budgetu), AC2 padne. To je nejsnazší chyba, které se vyhnout — `fast_tests`
  se dotýká výhradně `tests` budgetu.
- **Interakce s `nonconvergence_detected` / senior:** per-brána cap a
  fingerprint backstop jsou komplementární a jejich precedence se NEMĚNÍ.
  `derive_resume_point` nastaví `cap_reached` (na `next_phase=="developer"`)
  *před* tím, než `_override_phase` může developera přesměrovat na
  `senior`/`deadlock` — takže vyčerpaný budget má přednost před senior
  eskalací stejně jako dnes vyčerpaný `rounds_used`. Ověř, že tahle přednost
  zůstala zachovaná.
- **Bounded total práce:** protože review budget resetuje každý review
  approve, `MAX_LOOPS` sám o sobě neomezuje *celkovou* práci runu přes více
  úrovní — vyšší oscilace (rw2 nogo / authoritative fail opakovaně) drží
  `nonconvergence_detected` → senior → `ESCALATED_DEADLOCK` (2 rw2 nogo od
  posledního seniora, nebo shodný fingerprint). Tohle je **záměr**, ne díra;
  ověř, že to platí a je to zdokumentované, ne omylem.
- **Ověř oddělení labelu od capu:** `round` v log entries a commit messages
  musí zůstat monotónní přes celý run; žádný per-brána reset se do labelu
  nepromítá (AC7). `round` se nikde nečte zpět pro řídicí rozhodnutí.
- **Ověř „no new terminal":** grep, že `terminals.py` nemá přidaný stav a že
  cap všech čtyř bran ústí do `CAP_REACHED` s rozlišením přes `cap_cause`
  (AC10) — ne přes nový terminálový string.
- Reject, pokud se cap znovu opře o globální `rounds_used`, pokud `fast_tests
  pass` resetuje cokoli jiného než `tests`, nebo pokud kterákoli brána ztratí
  bound (nekonečná smyčka při trvalém selhání jedné brány).
