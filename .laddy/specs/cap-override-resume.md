---
type: feature
roles: [developer, rw1, rw2]
risk: high
status: draft-proposal
---
# cap-override-resume — Director `--continue`: znovu rozjet CAP_REACHED task s čerstvým budgetem

## Goal

Dát Directorovi explicitní, logovaný způsob, jak **znovu rozjet task, který
skončil na `CAP_REACHED`**, s čerstvým iteration budgetem — aniž by musel
editovat append-only log nebo měnit `MAX_LOOPS`.

Motivace vychází ze dvou vlastností dnešního (a nového per-brána) capu:

1. **`CAP_REACHED` je sticky.** `run()` volá `_recorded_terminal` jako první
   (`orchestrator/loop.py`) a při nalezeném sticky terminálu se okamžitě
   vrátí — loop se vůbec nerozjede. Prostý re-kickoff capnutého tasku tedy
   **no-opne**.
2. **Přehrání logu dá tentýž vyčerpaný budget.** I kdyby se sticky obešel,
   `derive_resume_point` přehraje celý log a napočítá znovu vyčerpaný budget
   (cap kápnul právě proto, že v historii *nebyl* pokrok, který by resetoval)
   → okamžitě zas `cap_reached`. Samotné zvednutí `MAX_LOOPS` nepomůže, protože
   sticky short-circuit je dřív než jakýkoli výpočet budgetu.

Řešení musí proto udělat **obojí naráz**: zneplatnit sticky terminál i vložit
reset bod pro budgety. Obé jedním event-sourced úkonem.

> **Závisí na:** `loop-budget-per-gate` (per-brána budgety + `cap_cause`
> v `ResumePoint`). Tento spec resetuje ty budgety; implementovat až po něm.
>
> Proto je označen `status: draft-proposal` — `kickoff cap-override-resume` ho
> odmítne. Spec je hotový a runnable; drží ho jen ta závislost. Až
> `loop-budget-per-gate` doteče do main, odstraň `status:` řádek a spusť.

## Root-cause context

- `_recorded_terminal` (`orchestrator/loop.py`) vrací terminál, když je
  `terminal_spec(outcome).sticky`. `CAP_REACHED` je `kind="failure"` →
  `sticky=True` → run se zastaví.
- `derive_resume_point` je čistá funkce; per-brána budgety (z
  `loop-budget-per-gate`) se počítají přes celý log. Existuje precedent pro
  „počítej jen od nějakého bodu": `nonconvergence_detected` počítá jen eventy
  **od posledního `senior` verdiktu** (senior intervence re-armuje backstop).
  Stejný pattern použijeme pro reset budgetů od Director override.
- Flagy (`task-flags`) zavedly „stav se derivuje z posloupnosti eventů, ne
  z mutovatelného záznamu" — override jde stejnou cestou: **nový append-only
  event**, žádné nové úložiště, žádná editace existujících řádků.

## Scope

**In:**

- **Nový log event `cap_override`** (append-only): `action="cap_override"`,
  `outcome="ok"`, povinné `reason` (neprázdné), `ts`. Zapisuje ho Director
  přes CLI; commitne se na `agent/<task>` větev jako ostatní artefakty.
- **`_recorded_terminal`**: `CAP_REACHED` přestane být sticky, pokud v logu
  existuje `cap_override` event **novější než ten `CAP_REACHED` terminál** →
  vrátí `None`, `run()` pokračuje. Žádný jiný terminál `cap_override`
  nezneplatní (path-guard, deadlock, quota, pushed/merge zůstávají beze změny).
- **`derive_resume_point`**: všechny čtyři per-brána budgety (review / tests /
  authoritative / dev_error) se počítají jen z eventů **od posledního
  `cap_override`** (slice jako u `nonconvergence_detected`/senior). `round`
  label (`rounds_used`, absolutní) se **NEresetuje** — zůstává monotónní.
- **CLI**: `orchestrator.run <task> --phase continue --reason "<text>"` —
  appendne `cap_override` (s reason), commitne, a spustí loop (detached, jako
  normální běh). Přeskakuje clarify/design (task už je rozpracovaný).
- **`kickoff.sh <task> --continue`** tenký passthrough, který předá reason do
  `--phase continue` a detachne loop (přežije SSH drop, vzor stávajícího
  kickoffu).
- **Viditelnost počtu override**: počet `cap_override` eventů se vypíše do
  `human-summary.md` a handbacku (např. „continued N×") a promítne do stavové
  zprávy při dalším `CAP_REACHED`.
- Testy pod `tests/` (čisté funkce + CLI passthrough se stubem).
- Doc: `SECURITY.md` / `USAGE.md` — jak `--continue` funguje a že je to
  vědomé opakovatelné Director rozhodnutí.

**Out:**

- **Žádný strop počtu override.** `--continue` jde použít neomezeně; pojistkou
  je manuální, logovaný akt s povinným reason a viditelný počet (rozhodnutí
  Directora). Tvrdá zeď se dá přidat později jako jedna podmínka v CLI — teď ne.
- **Žádné oslabení trust modelu.** `cap_override` re-armuje **jen** iteration
  budgety. Znovu rozjetý loop projde rw1/rw2/authoritative gaty úplně normálně;
  override **nepushuje do origin**, **neobchází žádný review/approval**, nemění
  merge policy ani `code_sha`/gate SHA logiku.
- **Žádný nový terminál.** Po vyčerpání čerstvého budgetu se zapíše zase
  `CAP_REACHED` (žádný nový stav v `terminals.py`).
- **Žádný reset `round` labelu** ani `nonconvergence`/senior slicing (ten dál
  řeže od posledního seniora, nezávisle na override).
- Žádná změna počítací mechaniky budgetů samotné (tu definuje
  `loop-budget-per-gate`) — tady se jen posouvá *počátek* počítání.

## Behaviour

`--continue` na tasku v `CAP_REACHED`:

1. **Validace (jinak refuse, non-zero, nic se nezapíše):** task existuje;
   jeho poslední terminál je `CAP_REACHED` (ne path-guard/deadlock/quota/
   pushed/merge — ty nejsou continuable); `--reason` je neprázdný.
2. Appendne `cap_override` event s `reason` a commitne ho na `agent/<task>`.
3. Spustí loop detached. `_recorded_terminal` teď kvůli novějšímu
   `cap_override` vrátí `None` → loop běží. `derive_resume_point` počítá
   budgety od override → všechny 0 → developer dostane čerstvý `MAX_LOOPS` na
   každou bránu.

**Opakování a idempotence** (load-bearing, přesně definované pořadí eventů):

- Log po jednom override: `… CAP_REACHED, cap_override` → continuable
  (override je novější než terminál) → loop běží.
- Když čerstvý budget zase dojde, zapíše se **nový** `CAP_REACHED` **za**
  `cap_override`: `… cap_override, …, CAP_REACHED`. Teď už novější
  `cap_override` za posledním terminálem NENÍ → sticky → loop zase stojí. Jeden
  override = jeden čerstvý běh k dalšímu capu.
- Druhý `--continue` appendne další `cap_override` za ten nový terminál →
  budgety se počítají od něj → zas fresh. Takto neomezeně.
- Pád uprostřed continue běhu + prostý re-kickoff: poslední terminál je starý
  `CAP_REACHED`, za ním je `cap_override` → `_recorded_terminal` vrátí `None` →
  loop normálně naváže (žádný nový override netřeba, resume je crash-safe).

**Reset bod (přesně):** `derive_resume_point` najde index posledního
`cap_override` a všechny čtyři budgety počítá jen z eventů za ním; `rounds_used`
(pro `round` label) dál počítá přes celý log. `cap_override` není v
`_PHASE_ACTIONS`, takže neovlivní výpočet `last`/`next_phase` transition —
next_phase se odvodí z posledního reálného fázového eventu (např. poslední
`rw1 changes_requested` → `developer`), a s vynulovanými budgety developer běží.

## Acceptance criteria

Testy nad čistými funkcemi (`_recorded_terminal`, `derive_resume_point`) staví
log z fake entries jako ve stávajícím `tests/test_loop_resume.py`; CLI se
testuje passthrough stubem jako u ostatních launcherů.

1. **Sticky se zruší jen pro CAP_REACHED s novějším override.** Log končící
   `[…, terminal CAP_REACHED, cap_override]` → `_recorded_terminal(...) is
   None`. Bez toho `cap_override` → vrací `"CAP_REACHED"` (beze změny).
2. **Override neplatí na jiné terminály.** Log `[…, terminal
   PATH_GUARD_VIOLATION, cap_override]` → `_recorded_terminal` stále vrací
   `"PATH_GUARD_VIOLATION"` (a totéž pro `ESCALATED_DEADLOCK`, `PUSHED`).
3. **Nový CAP za override zase drží.** Log `[…, cap_override, …, terminal
   CAP_REACHED]` (override starší než terminál) → `_recorded_terminal` vrací
   `"CAP_REACHED"`.
4. **Budgety se resetují od override.** Log, který by byl `cap_reached`
   (`max_loops=3`, tři `rw1 changes_requested` s `fast_tests pass` mezi nimi),
   následovaný `cap_override` → `derive_resume_point(...).phase == "developer"`
   (ne `cap_reached`), `cap_cause is None`.
5. **Čerstvý budget je přesně MAX_LOOPS.** Po `cap_override` tři nové
   `rw1 changes_requested` (max_loops=3) → zase `cap_reached`,
   `cap_cause == "review"`. Druhý `cap_override` za tím → opět `developer`
   (opakovatelnost).
6. **Reset platí pro všechny brány.** Analogický test k AC4/5 pro `tests`,
   `authoritative` i `dev_error` budget: `cap_override` vynuluje každý z nich.
7. **`round` label se override NEresetuje.** V logu s override roste
   `ResumePoint.round` monotónně s celkovým počtem developer kol napříč
   override (žádný skok zpět).
8. **CLI validace.** `--phase continue`:
   - bez `--reason` (nebo prázdný) → non-zero, `cap_override` se NEzapíše;
   - na tasku, jehož poslední terminál není `CAP_REACHED` → non-zero, jasná
     zpráva, nic se nezapíše;
   - na neznámém/nespuštěném tasku → non-zero, nic se nezapíše.
   Každý případ zvlášť.
9. **Happy path CLI.** `--phase continue --reason "x"` na `CAP_REACHED` tasku
   appendne právě jeden `cap_override` s `reason=="x"` a pak spustí loop
   (ověřeno stubem zachycujícím append + fázi; žádné reálné LLM/git push).
10. **Počet override je vidět.** `human-summary.md`/handback po dalším
    `CAP_REACHED` obsahuje počet `cap_override` eventů (např. „continued 2×") —
    pokryto testem nad summary/handback cestou.
11. **Trust nedotčen.** Grep/test: `--phase continue` cesta nevolá push do
    origin, neobchází rw1/rw2/authoritative (znovu rozjetý loop jimi prochází),
    nezavádí nový terminál v `terminals.py`, nemění merge-decision/gate SHA
    logiku. Jediný efekt `cap_override` je posun počátku budgetů.
12. **Crash-safe resume.** Po `cap_override` a „pádu" (žádný nový terminál)
    vede prostý `derive_resume_point`/`_recorded_terminal` k pokračování loopu,
    ne k zastavení — pokryto testem.
13. Suite green: `pytest -n auto -q`, `ruff check .` clean, `basedpyright`
    clean pro dotčený scope.

## Notes for the reviewer

- **Ověř „obojí naráz":** override musí zároveň (a) zneplatnit sticky
  a (b) resetovat budgety. Kdyby dělal jen (a), loop se rozjede a *okamžitě*
  zas capne (AC4 by chytlo regresi). Kdyby jen (b), sticky short-circuit ho
  nepustí (AC1).
- **Ověř pořadí eventů (AC1 vs AC3):** rozhoduje, jestli je `cap_override`
  **novější** než poslední `CAP_REACHED`. „Novější" = pozdější pozice v
  append-only logu; implementace nesmí záviset na `ts` parsování (log je
  uspořádaný appendem). Jeden override pokryje právě jeden běh do dalšího capu.
- **Ověř oddělení od `round` a od nonconvergence:** override resetuje jen ty
  čtyři budgety. `round` label zůstává absolutní (AC7); `nonconvergence_detected`
  dál řeže od posledního seniora, ne od override (senior backstop se nesmí
  override rozbít ani obejít).
- **Ověř trust (AC11) obzvlášť pečlivě:** tohle je high-risk cesta. Override
  smí re-armovat *pouze* iteration budget. Žádný push do origin, žádné obejití
  reviewerů, žádná změna merge policy. Znovu rozjetý task musí projít stejnými
  gaty jako čerstvý. Nález opaku = CHANGES_REQUESTED.
- **Ověř refuse větve (AC8):** `--continue` na ne-CAP_REACHED tasku (zejména
  `PATH_GUARD_VIOLATION` a `ESCALATED_DEADLOCK`) musí odmítnout a **nic
  nezapsat** — dvojitá pojistka: refuse v CLI + `_recorded_terminal`
  zneplatňuje jen `CAP_REACHED` (AC2).
- Reject, pokud override pushuje/mergeuje, obchází review, resetuje `round`
  nebo nonconvergence, zavádí nový terminál, nebo pokud po override loop
  okamžitě re-capne (chybějící reset budgetů).
