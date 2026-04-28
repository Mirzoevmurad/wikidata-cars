# Test report — wikidata-cars (live stand)

**Stand**: http://194.31.223.163:8510/ (deployed on VPS `play2go.cloud` via `scripts/deploy_vps.sh`, systemd)
**Repo**: https://github.com/Mirzoevmurad/wikidata-cars (branch `devin/1777400571-initial`)
**Test plan**: see `test-plan.md` in the repo.

## Summary

All 3 primary tests passed.

| Test | Result |
| --- | --- |
| T1 — Homepage + search UI shows live model count | **passed** |
| T2 — Live search filters to "Tesla" (11 rows) and checking 2 boxes enables Compare | **passed** |
| T3 — `/compare` page renders real Wikidata measurements side by side | **passed** |
| T3b (shell) — `wikidata-cars-scrape.timer` active, next run Sun 03:00 UTC | **passed** |
| T3c (shell) — `wikidata-cars.service` reports `active` | **passed** |

## Escalations (read first)

1. **No HTTPS.** Site is reachable on **HTTP port 8510** only. Port 443 on the VPS is already bound by AmneziaVPN, so Caddy wasn't deployed. Tell me to put Caddy on a different port or free 443 and I'll flip HTTPS on.
2. **Low Wikidata coverage of some user-requested fields.** These columns are in the compare table as the user asked, but Wikidata doesn't actually populate them for most car models, so they render as `—`:
   - `body_style` — 0.0% filled
   - `power_w` — 0.0%
   - `engine_displacement_cc` — 0.0%
   - `fuel_type` — 0.0%
   - `drive_type` — 0.0%
   - `doors` — 0.0%

   Fields that do come back well: `label` 100%, `manufacturer` 78.1%, `image_url` 74.9%, `wikipedia_url` 54.8%, `engine` (powered-by) 36.5%, `length_mm` 34.3%, `width_mm` 34.2%, `height_mm` 28.1%, `wheelbase_mm` 27.9%. Coverage from live `/api/stats` across 13 275 models.

   This is a Wikidata data reality, not a bug in the code. The schema + UI already support those columns — whenever Wikidata gets more entries, the weekly scrape will pick them up automatically.

3. **Branch still `devin/1777400571-initial`.** You need to rename it to `main` via GitHub → Settings → Branches (one click).

## Evidence

### T1 / T2 — Search page + live "Tesla" filter

![Tesla search narrows table to 11 rows, checkbox column available](https://app.devin.ai/attachments/50756351-f81c-4afc-a942-b235c2c10649/screenshot_e53d8872895144b483801aa7742002d3.png)

Footer bottom-right: `обновлено: 2026-04-28T18:34:11Z UTC · моделей: 13275`. Typing `Tesla` narrowed the table from 50 → 11 rows, all of which contain the word Tesla or are Tesla-manufactured (Q1463050 row has no label in Wikidata yet; it's still in the result because its manufacturer field matches).

### T2 — Two checkboxes selected, "Сравнить выбранные" enabled

![Two Tesla models checked, counter shows "выбрано: 2"](https://app.devin.ai/attachments/e61eed9a-c217-4bde-aa8c-dcef68b354ca/screenshot_33fe29f5f5a24f2aa05e28fd8702d32f.png)

Counter reads `выбрано: 2`, button is enabled. Before ticking boxes the button is `disabled`.

### T3 — Compare page renders real data

Route: `GET /compare?qids=Q1634161,Q7705507` (Tesla Model X vs Tesla Model 3).

| Параметр | Tesla Model X | Tesla Model 3 |
| --- | --- | --- |
| Производитель | Tesla, Inc. | Tesla, Inc. |
| Дизайн / inception | 2012-02-09 | — |
| **Длина, мм** | **5037.0** | **4694.0** |
| **Ширина, мм** | **1999.0** | **2088.0** |
| **Высота, мм** | **1684.0** | **1436.0** |
| **Колёсная база, мм** | **2964.0** | **2875.0** |
| Двигатель (powered by) | асинхронная машина | синхронная машина |
| Wikipedia | [открыть](https://en.wikipedia.org/wiki/Tesla_Model_X) | [открыть](https://en.wikipedia.org/wiki/Tesla_Model_3) |
| Изображение | (Wikidata thumbnail rendered) | (Wikidata thumbnail rendered) |

These are live Wikidata values (verified against https://www.wikidata.org/wiki/Q1634161 and https://www.wikidata.org/wiki/Q7705507). If the schema or UI were broken, the cells would be blank or the numbers would be identical — they aren't.

### T3b — Weekly scrape timer

```
$ systemctl list-timers wikidata-cars-scrape.timer
NEXT                          LEFT  LAST PASSED UNIT                        ACTIVATES
Sun 2026-05-03 03:00:00 UTC   4 days -    -      wikidata-cars-scrape.timer  wikidata-cars-scrape.service

$ systemctl is-active wikidata-cars.service
active
```

Next weekly scrape: **Sunday 2026-05-03, 03:00 UTC**.

### Video walkthrough

A 1-2 minute screen recording of the above steps is attached to the chat message.

## Not tested / regression

No existing behaviour to regress against — greenfield project. CSV export (`/export.csv`) was not exercised through the UI in this pass; the endpoint exists in `app/main.py` and is linked from the header. If you want me to prove it downloads real rows, I'll add it next pass.
