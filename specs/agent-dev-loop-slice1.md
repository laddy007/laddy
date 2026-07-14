---
status: done
---
# agent-dev-loop-slice1 — Python orchestrátor: clarify gate → developer ⇄ fast testy ⇄ rw1 → push branch (spike)

## Authoritative design

> **Design doc (závazný pro architekturu a pojmy):**
> `docs/development/superpowers/specs/2026-07-05-agent-dev-loop-design.md`
> — zejména §5 (loop), §9 (artefakty + verdikt schéma), §11 (komponenty),
> §12 (build order / Slice 1), Appendixy D+E (rozhodnutí).

Tento spec definuje **co a proč + akceptaci** pro Slice 1. Design doc drží
architekturu; při konfliktu eskaluj, nereinterpretuj.

## Goal

Postavit **Slice 1 (spike)** nového autonomního dev-loopu: Python orchestrátor
v `.laddy/orchestrator/`, který jeden task provede přes:

```
clarify gate (interaktivní) → developer (claude -p) ⇄ fast testy ⇄ rw1 review
(schema-validovaný verdikt) → push agent/<task> branch
```

Účel spike: dokázat session resume, strukturované verdikty, spouštění testů a
git ops v Pythonu — a připravit půdu pro měření konvergence na 2–3 reálných
myapp taskách (měření samotné dělá Director, není součást akceptace).

## Non-goals (VERBATIM z design docu §12 — nestavět dopředu)

**Slice 1 explicitly does NOT include:** rw2, senior reviewer, auto-merge,
ntfy, report-only task types, GitHub App.

Navíc out of scope:

- Žádná merge logika nikde (`gitops` umí clone/fetch/branch/commit/push — NE
  merge, viz §11).
- Žádný authoritative Docker gate (Slice 2) — jen fast inner testy.
- Žádný oscillation fingerprint (Slice 2) — jen prostý iteration cap.
- Žádná role palette / spec-declared composition (Slice 3) — natvrdo
  `developer → rw1`.
- Nerušit stávající `agent-flow.sh` / `agent-run.sh` / `agent-cycle.sh` — do
  `2del/` odejdou až po ověření Slice 1 na reálných taskách, ne v tomto tasku.

## Scope

1. **`.laddy/orchestrator/`** (Python, viz §11): `run.py` (entrypoint),
   `loop.py` (sekvence + iteration cap), `clarify.py` (interaktivní clarify
   gate, append `## Clarifications` do specu), `agents.py` (runner pro
   `claude -p` / `claude -p -r` → typovaný výsledek: session id, exit reason;
   rozhraní vendor-agnostické, implementovaný jen Claude), `verdict.py`
   (JSON schéma + validace + retry na malformed), `tests.py` (fast inner testy
   dle `TEST_COMMANDS` konfigurace), `gitops.py` (clone/fetch z GitHubu,
   branch `agent/<task>`, commit, push — bez merge).
2. **Role prompty** pro developera a rw1 v `.laddy/roles/` (extrakce/úprava ze
   stávajících v `agent-flow.sh` — head start, ne greenfield).
3. **Artefakty Slice 1** pod `.laddy/tasks/<task>/`: `spec` kopie,
   `iteration-log.jsonl` (append-only), `reviewer-a-verdict.json`,
   `human-summary.md`. Stav loopu je rekonstruovatelný z artefaktů (crash →
   resume z artefaktů; `-r` je jen token optimalizace, §9).
4. **Verdikt schéma** přesně dle §9 včetně validačního pravidla z Appendixu E:
   `severity: advisory` vyžaduje prázdné `failure_scenario`; finding s
   konkrétním `failure_scenario` musí být `blocker`. Malformed verdikt →
   retry s chybovou zprávou (bounded).
5. **Iteration cap** `MAX_LOOPS` default 4; po vyčerpání zastavit a zapsat
   `human-summary.md` se stavem (bez fingerprint logiky).
6. **`kickoff.sh`** tenký bash launcher (VPS entrypoint): zavolá orchestrátor
   (clarify fáze interaktivně v terminálu, pak detached běh loopu —
   `agent-run.sh` vzor: přežije SSH drop).
7. **Unit testy orchestrátoru** v `tests/` (běží v normální pytest suite):
   verdikt validace (vč. advisory/failure_scenario pravidla), loop sekvence
   s fake agent runnerem (žádné reálné LLM volání v testech), resume z
   artefaktů, cap.

## Constraints

- Bash jen launcher/bootstrap; veškerá policy/stav/rozhodnutí v Pythonu (§11).
- Sessions `S_dev` a `S_rw1` oddělené, nesdílí paměť; verdikt putuje jako data
  v promptu (context as data, §5).
- Terminální akce = push `agent/<task>` na GitHub (RW deploy key). PR otevřít
  jen pokud je na boxu dostupný `gh`/token; jinak vypsat compare URL do
  human-summary — deploy key sám PR otevřít neumí (poznámka pro plán; App až
  Slice 4).
- Minimální závislosti; nové Python deps do `requirements-dev.txt`, ne do
  produkčního `requirements.txt`.
- Kód/commity/docstringy anglicky; LF konce řádků; ASCII-safe skripty.
- Green: `ruff check .` clean, `basedpyright` clean pro dotčený scope,
  `pytest -n auto -q` zelené.
- TDD pro orchestrátor logiku (verdict validace, loop sekvence, cap, resume).
- Nemergovat do `main`, nepushovat (Tier 3 — Director).

## Acceptance criteria

1. `kickoff.sh <task>` na čerstvém klonu provede: clarify gate (interaktivní;
   varianta „no questions" projde bez zásahu) → detached loop → developer
   implementace → fast testy → rw1 review → při APPROVED push `agent/<task>`.
2. `reviewer-a-verdict.json` je schema-validní; validátor odmítne (a retryne)
   malformed verdikt i advisory finding s neprázdným `failure_scenario` —
   pokryto unit testem.
3. Po zabití procesu uprostřed běhu jde tentýž task znovu spustit a loop
   naváže z artefaktů (ne ze session) — pokryto testem s fake runnerem.
4. `MAX_LOOPS=4` cap: po 4 kolech bez APPROVED se loop zastaví a
   `human-summary.md` obsahuje stav + co bylo zkoušeno.
5. `iteration-log.jsonl` má jeden append-only řádek na akci/kolo; žádný
   UPDATE existujících řádků.
6. V kódu Slice 1 neexistuje: merge operace, rw2/senior role, ntfy volání,
   auto-merge/policy engine, report-only typy (grep-ověřitelné).
7. `pytest -n auto -q` zelené (vč. nových unit testů), `ruff check .` clean,
   `basedpyright` clean pro dotčený scope; stávající `.laddy/scripts/*`
   nedotčené.

## Notes for the reviewer

- **Ověř non-goals:** žádný kód pro rw2/senior/auto-merge/ntfy/report-only/
  GitHub App — nález = CHANGES_REQUESTED (design §12 to zakazuje explicitně).
- **Ověř „no merge":** `gitops.py` neobsahuje žádnou merge/PR-merge operaci;
  push jde výhradně na `agent/<task>` ref.
- **Ověř artifact-first stav:** resume čte artefakty, ne session; `-r` je
  optimalizace, jejíž výpadek běh nezabije (§9).
- **Ověř verdikt pravidlo:** advisory ⇒ prázdné `failure_scenario` je vynucené
  ve validátoru + testem, ne jen v promptu (Appendix E bod 3).
- **Ověř oddělené sessions** dev vs rw1 (žádné sdílené session id).
- **Ověř, že bash zůstal tenký:** `kickoff.sh` jen bootstrap/exec; žádná
  policy v bashi.
- Reject, pokud loop v testech volá reálné LLM, pokud stav žije jen v session,
  nebo pokud se stavělo za hranici Slice 1.
