# dobrewoks1.github.io

Страницы для откликов и небольшие рабочие инструменты. Собрано в связке с Claude Code.

- `sellehome/` — страница-ответ на вакансию: база Telegram-каналов и чатов продавцов Ozon/WB с живыми метриками,
  оценкой и черновиками первых сообщений. Данные: `sellehome/channels.json`.
- `tools/collect_tg.py` — сбор публичных данных о каналах с t.me (без API и логина, 1 запрос/с).
  `python tools/collect_tg.py tools/channels.txt sellehome/channels.json`

Данные взяты с публичных страниц t.me на дату сбора и могут устареть.
