# Test plan — wikidata-cars

**Live URL**: http://194.31.223.163:8510/ (deployed on `play2go.cloud` VPS, HTTP only, port 8510).
**What's being proven**: a user can search the Wikidata-sourced car database by model name and compare characteristics of multiple picks in one table. Weekly refresh timer is configured.

Each test step below lists an **exact expected value** that would differ if the feature were broken.

## Primary flow

### T1. Homepage + search (FTS) against live data
1. Navigate to `http://194.31.223.163:8510/`.
2. **Assertion A** — page loads with HTTP 200, shows header "🚗 Wikidata Cars", heading "Поиск модели", search input and a non-empty result table.
3. **Assertion B** — footer shows either "обновлено: <ISO timestamp>" or "моделей: <number>". The number must be > 0. (Verified earlier via `/api/stats` — currently ~640 and growing.)
4. Type `Tesla` into the search box.
5. **Assertion C** — `/api/search?q=Tesla` returns ≥1 row whose `label` contains "Tesla" (case-insensitive). The rendered table body must show at least one row with the word "Tesla" in the Model column. If nothing matches (small initial scrape sample), retry with `BMW` — must show ≥1 BMW row (confirmed via `/api/search?q=bmw`).

### T2. Multi-model compare table
1. On the search page, tick checkboxes for exactly **two** different car models.
2. **Assertion D** — the counter under the table must change to "выбрано: 2" and the "Сравнить выбранные" button must become enabled (attribute `disabled` removed).
3. Click **Сравнить выбранные**.
4. **Assertion E** — browser URL becomes `/compare?qids=<qid1>,<qid2>` with exactly the two selected QIDs, and the page title contains "Сравнение".
5. **Assertion F** — compare table has header columns `Параметр`, plus one column per selected model with the model label as the header. Row labels must include the expected user-requested fields: `Производитель`, `Масса, кг`, `Мощность, Вт`, `Длина, мм`, `Ширина, мм`, `Высота, мм`, `Объём двигателя, см³`, `Тип топлива`, `Привод`, `Количество дверей`, plus the `Обновлено` (timestamp) row.
6. **Assertion G** — at least one numeric cell (e.g. `Длина, мм` or `Ширина, мм`) must show a real number for at least one of the two chosen models. (Empty cells are rendered as `—`; we must see at least some real data for the chosen pair. If the pair chosen has all blanks, retry with BMW models which are known to have length/width.)

### T3. Weekly refresh wiring (non-UI, shell)
1. SSH to VPS, run `systemctl list-timers wikidata-cars-scrape.timer --no-pager`.
2. **Assertion H** — output contains "Sun" and the `UNIT` column equals `wikidata-cars-scrape.timer`. Expected next run: `Sun 2026-05-03 03:00:00 UTC` (already verified once; re-verify during execution).
3. Also run `systemctl is-active wikidata-cars.service` — **Assertion I** — must print `active`.

## Regression — not in scope

No existing behavior to regress against (greenfield repo + new VPS service). Not testing.

## Known limitations noted in plan (so reviewer isn't surprised)

- HTTPS not configured — VPS port 443 is already used by AmneziaVPN, so site is HTTP-only on :8510. Will mention in the report.
- Wikidata coverage of some user-requested fields (body_style, power_w, fuel_type, drive_type, doors, engine_displacement) is ~0% because those Wikidata properties are barely populated for car models. Compare UI still renders the rows (with `—` placeholders) so when/if Wikidata fills them, they show up automatically. This is a data reality, not a code bug — will call it out explicitly in the report.
- Initial scrape is still in progress on the VPS during T1-T2; only a slice of models is searchable. The weekly timer will top it up on its next run.
