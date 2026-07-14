---
status: done
---
# game-type-payload-convergence — `game_type` (+ 8 identity/config polí) má jeden domov: sloupec, ne payload

## Execution — subagent-driven

**Tento spec se implementuje PŘES SUBAGENTY.** Krok za krokem je rozepsaný v
plánu:

> **Plán (autoritativní step-by-step, TDD, bite-sized):**
> `docs/development/superpowers/plans/2026-07-04-game-type-payload-convergence.md`

Použij `superpowers:subagent-driven-development` — **jeden čerstvý subagent na
task** (Task 1–4 v plánu), s review mezi tasky. Každý task končí nezávisle
testovatelným deliverable a vlastním commitem (viz kroky v plánu). Tento spec
definuje **co a proč + akceptaci**; plán definuje **jak** (přesné soubory,
kód, příkazy). Při konfliktu mezi tímto specem a plánem drží tento spec záměr
a akceptaci, plán drží mechaniku — nesmí si odporovat; když ano, eskaluj.

## Goal

`game_type` a dalších 8 **identity/config** polí je dnes uloženo **dvakrát**:
jako sloupec v `game_instance` a zároveň jako klíč v JSON payloadu
(`asset_version.payload` a `game_version.payload`). Obě kopie se rozešly:
migrace `81e2ef8b7197` přejmenovala hodnotu `mikulov→base` jen ve sloupci a
JSON twin nechala, takže `_extract_game_fields` (`myapp/application/games/_impl.py`)
při parsování stale hodnoty spadne na
`ValueError: 'mikulov' is not a valid GameType` (živě padá `/en/creating`).

Cíl: **zkonvergovat těchto 9 polí na jediný domov = `game_instance` sloupec.**
Přestat je zapisovat do payloadu, přestat je z payloadu číst a odstranit je z
existujících payloadů datovou migrací. Tím padne i ten živý crash.

**9 konvergovaných polí (single home = sloupec):**
`game_type`, `navigation_mode`, `map_provider`, `map_style`,
`map_initial_zoom`, `game_mode`, `interaction_mode`, `progress_owner`,
`skin_id`.

**4 pole, která ZŮSTÁVAJÍ v payloadu (version-semantic — NESAHAT):**
`name`, `intent`, `access_policy`, `price_minor` (+ obsah: `tasks`,
`cover_image_url`). Mají snapshot sémantiku publikované verze (public view je
čte přes `prefer_payload`), tj. jejich duplikace je záměrná, ne dluh.

## Scope

**In scope — přesně dle plánu (Task 1–4):**

1. **Read-side** (`_impl.py`): `_extract_game_fields`, `_merge_game_fields` a
   `_build_game_list_item` řeší 9 polí **jen ze sloupce** `aggregate.game.*`;
   smazat divergentní inline merge v `_build_game_list_item`
   (§4.6 R1 — druhá implementace téhož merge). Regresní test na legacy
   `game_type="mikulov"` payload.
2. **Write-side** (`_impl.py`): `_build_game_payload` přestane 9 polí
   zapisovat; upravit všechny 3 call-sites (create, update, photo-import).
   Sloupcové zápisy (`GameRecord(... game_type=... )`) **zůstávají**.
3. **Datová migrace** (nová alembic revize navazující na head `bc5f42985725`):
   stripne 9 klíčů z `asset_version.payload` i `game_version.payload` (oba
   `TEXT` s JSON). `downgrade()` = dokumentovaný no-op (data redundantní se
   sloupcem, staré čtení bylo column-first). Data-only, žádná změna schématu.
4. **Konvergenční guard** v `tests/test_architecture_contracts.py` (payload
   nikdy nenese 9 polí — §4.6) + sweep testů, které asertují starý payload
   shape.

**Out of scope:**

- Nesahat na 4 version-semantic pole (`name`/`intent`/`access_policy`/
  `price_minor`) ani na jejich payload kopie.
- Neměnit `game_instance` schéma (sloupce jsou už zdroj pravdy) — změna je
  **data-only**, `tests/test_schema_matches_orm.py` musí zůstat zelený.
- Neřešit rozdělení dataclassu `GameFields` na content vs resolved (follow-up
  v plánu).
- Nespouštět migraci proti staging/prod (Tier 3 — Director). Nemergovat do
  `main`, nepushovat.

## Constraints

- Business logika v `myapp/models.py`; `application/` orchestruje,
  `infrastructure/` jsou adaptéry (CLAUDE.md §4.1). Boundary
  application -> infrastructure zůstává čistá (§13).
- Hand-written datová migrace je povolená (CLAUDE.md §9), ale **musí mít v
  souboru zdůvodnění**. Nikdy `alembic stamp`. Preflight
  `python scripts/preflight_alembic.py` musí projít.
- Tests jen proti PostgreSQL (`docker compose up -d db`), izolované schéma na
  test přes `db` fixture.
- **LF** konce řádků, ASCII-safe (repo vynucuje přes `.gitattributes` +
  `tests/test_line_endings.py`).
- Jazyk: kód/testy/commit messages/migrace anglicky; tento spec je česky.
- Green pro každý commit dotčeného scope: `ruff check .` clean,
  `basedpyright` clean pro dotčený scope, `pytest -n auto -q` zelené.
- TDD: každý task nejdřív failing test, pak minimální implementace (viz plán).

## Acceptance criteria

1. `/en/creating` (i list vlastních her) se načte bez
   `ValueError: 'mikulov' is not a valid GameType`; hra s legacy
   `game_type="mikulov"` v payloadu se vylistuje a `game_type` se bere ze
   sloupce (regresní test v `tests/test_game_type_payload_convergence.py`).
2. `_build_game_payload(...)` výstup **neobsahuje žádné** z 9 konvergovaných
   polí (guard v `tests/test_architecture_contracts.py`).
3. Žádný read path (`_extract_game_fields`, `_merge_game_fields`,
   `_build_game_list_item`) nečte 9 polí z payloadu; hodnoty jdou ze sloupce.
   Inline `game.X or payload.X` merge v `_build_game_list_item` je odstraněn.
4. Datová migrace odstraní 9 klíčů z `asset_version.payload` i
   `game_version.payload`; `preflight_alembic.py` projde; `alembic upgrade
   head` proběhne lokálně; `tests/test_schema_matches_orm.py` zelený.
5. Version-semantic pole (`name`/`intent`/`access_policy`/`price_minor`)
   zůstávají v payloadu beze změny; jejich testy nejsou dotčené.
6. `pytest -n auto -q` zelené; `basedpyright` + `ruff check .` clean pro
   dotčený scope. Case testů, které asertovaly starý dual-write payload
   shape, upraveno tak, aby četly pole z DTO/sloupce.
7. Změněné soubory: `myapp/application/games/_impl.py`, nová
   `alembic/versions/*_strip_converged_fields_from_game_payload.py`,
   `tests/test_game_type_payload_convergence.py` (nový),
   `tests/test_architecture_contracts.py`, plus nutné úpravy dotčených testů.
   Žádná změna produktového kódu mimo `_impl.py` (+ migrace).
8. Nemergnuto do `main`, nepushnuto; migrace nespuštěna proti cloudu.

## Notes for the reviewer

- **Ověř single-home:** grepni 9 klíčů v `_build_game_payload` výstupu i v
  read pathech — payload je nesmí nést ani zapisovat. Guard test to zamyká.
- **Ověř, že sloupcové zápisy zůstaly** (`GameRecord(... game_type=... )` v
  create/update, `games.insert(... game_type=content.game_type ...)` v clone)
  — konvergujeme na sloupec, ne pryč od něj.
- **Ověř migraci:** stripuje oba TEXT payload sloupce, má zdůvodnění, down je
  vědomý no-op; žádná změna schématu; navazuje na `bc5f42985725`.
- **Ověř, že version-semantic pole zůstala** v payloadu (public snapshot
  sémantika `prefer_payload`) — jejich odstranění = CHANGES_REQUESTED.
- **Ověř §4.6 konvergenci:** inline divergentní merge v
  `_build_game_list_item` je pryč, ne jen okomentovaný.
- **Ověř scope:** jediný dotčený produktový soubor je `_impl.py` (+ migrace);
  testy dle plánu; nemergnuto/nepushnuto; migrace nespuštěna proti cloudu.
- Reject, pokud crash přetrvává, pokud 9 polí dál žije v payloadu, pokud se
  sáhlo na version-semantic pole, nebo pokud se konvergovalo mimo sloupec.

## Oprava po lokální verifikaci (round 2 — ZÁVAZNÉ)

Plný lokální test suite (Python 3.11, PostgreSQL) odhalil regresi, kterou
předchozí review minul: **`tests/test_session_snapshot_capture.py::
test_snapshot_map_settings_read_from_payload_not_dead_column` selhává**
(`assert None == 'mapbox'`).

**Příčina:** změna `_serialize_game_version` v
`myapp/application/games/snapshot.py` teď čte `map_provider` / `map_style` /
`map_initial_zoom` z `game_instance` sloupce (`game.map_provider`) — to je
**správně** a v souladu s konvergencí (migrace `f7a3c1d9b2e4` map z payloadu
odstraňuje). Ale tento **existující** test pořád asertuje starý model (map se
čte z `game_version.payload`, denormalizované sloupce NULL) a **nebyl
zaktualizován** (jiné stale testy upravené byly, tenhle přehlédnut).

**Co udělat — a co NE:**

- **NEREVERTUJ** `_serialize_game_version` zpět na čtení z payloadu. Rozbilo by
  to konvergenci — po migraci payload map pole nenese, takže payload-read by
  vracel `None` pro každou hru.
- **Přepiš `test_snapshot_map_settings_read_from_payload_not_dead_column`** na
  konvergovaný model: seedni `game_instance.map_provider/map_style/
  map_initial_zoom` (sloupec = single home), NE `game_version.payload`;
  assertuj, že snapshot vrací hodnoty ze sloupce; docstring přepiš na nový
  model (map config žije v `game_instance` sloupci, čte se live; `game_version`
  payload ani jeho sloupce už nejsou zdroj).
- **Ověř populaci sloupce:** potvrď (a případně doplň test/publikační cestu),
  že published hra má `game_instance.map_provider` naplněný — tj. read-from-
  column nedává `None` na reálných datech. Pokud publish/clone cesta sloupec
  neplní, doplň to (je to součást konvergence, ne mimo scope).
- **Ignoruj z lokálního failure reportu** 9× `ImportError` (`StrEnum`,
  `tomllib`) a `test_dev_script_taskkill` — to byly **WSL-specifické** artefakty
  (Python 3.10 / Windows PowerShell z ext4 cesty), na VPS (Python 3.11, Linux)
  nenastanou. Neřeš je.

**Nová akceptace #9:** `test_snapshot_map_settings_read_from_payload_not_dead_column`
(nebo přejmenovaný nástupce) je zelený a asertuje čtení map polí ze
`game_instance` sloupce; `_serialize_game_version` NEbyl vrácen na payload-read;
plný `pytest -n auto` zelený na Pythonu 3.11.

**Pro reviewera:** ověř, že fix je **přepis testu na column-read model**, ne
revert snapshotu na payload. Reject, pokud se `_serialize_game_version` vrátil
ke čtení z payloadu, nebo pokud test zůstal na starém payload-modelu.
