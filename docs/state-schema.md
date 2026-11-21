# Контракт памяти v1

Импорт дополняет этот контракт: `indexed` — ещё не прочитан, `read_excerpt` — декларация прочтения выбранного диапазона. `original_locator/original_sha256` и `index_locator/index_sha256` защищают снимок и извлечение; `reviewed_excerpts` хранит точное место, диапазон, preview цитаты и hash полного диапазона. Активные выводы не могут зависеть от indexed. Исходный формат, частичность и ограничения — в index.json/note.md. Прочие поля и правила прежние; см. docs/import.md.

state.json: question, stage, summary, next_step; sources, claims, decisions, history — массивы. Каждый узел имеет уникальный во всём проекте id, type, status и depends_on (массив существующих ID, без циклов). У claims есть строковый text; у sources — локальный locator внутри проекта и sha256. URL хранится в заметке и реестре.

Рекомендуемые статусы источника: read, checked, synthetic, stale. Утверждения: checked, unverified, unknown, stale. Решения: proposed, conditional_proposal, accepted, stale, superseded. Типы: own_material, public_documentation, calculation, fact_about_file, hypothesis, forecast, unknown, decision. validate проверяет типы, ISO-дату доступа, ID реестра, URL в заметке, SHA-256, доступность файлов и stale-предков активных узлов; не истинность и не полноту оснований всего текста. URL при validate повторно не загружаются.

sources/register.csv: id,type,locator,accessed_at,location,claim_ids,limitations. В реестре locator может вести на URL, в state — на заметку с тем же URL. location содержит раздел/строку, accessed_at — дату чтения, не период наблюдений.

events.jsonl записывается через log: at (часы хоста UTC), event, model, tool, elapsed_seconds, cost, currency, retries, human_minutes. При расхождении часов дата сессии указывается в событии. Неизвестная цена — unknown, неизвестное время — null; это не ноль. costs суммирует записанные операции по валютам, не заменяет счёт поставщика.

decide принимает JSON внутри проекта по templates/approval.json. Заполнять только после выбора человека: quote — реплика без персональных данных, owner_role — роль, rationale — основания, metric, decided_at, review_date. Обязательства: id, text, owner_role, due_date, metric, status. human_decision=true не аутентифицирует человека технически: модель не вправе выдумывать такую запись. Полная запись сохраняется снимком в history.

stale сохраняет прежние узлы целиком в history.previous_nodes и транзитивно меняет статусы. Перед правкой файлов сохранять версии в evidence/revisions/. Затем агент перечитывает материалы, обновляет стратегию и просит пересмотр. stale не принимает нового решения. review показывает сроки и stale по запросу; фонового мониторинга нет.

checkpoint запускает validate до смены этапа. После изменения источника старый hash не обновляется автоматически: сначала stale и сохранение версии, затем явное перечитывание, новая карточка/hash/статусы и запись причины в history. Нельзя снять stale только ради прохождения теста.

Для накопительной стоимости log --cost-scope cumulative сохраняет снимок. costs берёт последний снимок по каждой валюте и прибавляет только более поздние операции, а не суммирует снимки дважды. Это ресурсная оценка при валюте CREDITS_STANDARD_EQ, не денежный счёт.
