# Odysseus — большой TODO для агентного режима

Исследование: 22 сентября 2026. Это backlog, не утверждение о готовности функций.
`P0` — надёжность долгого run; `P1` — качество и скорость; `P2` — расширения.
Похожие возможности в Odysseus отмечены как «расширить», чтобы не дублировать
существующие Goal, Plan, Team, MCP, file tools, context ledger и checkpoints.

## Текущий журнал реализации

Каждый чекбокс ниже остаётся открытым до проверки всех частей требования на
реальном runtime. Локальные тесты сами по себе не закрывают пункт.

- 2026-09-23 11:58 +05: пункт №30 подтверждён отдельно от шестичасового gate.
  `tests/fixtures/long_replay_corpus_manifest.json` фиксирует SHA-256 corpus
  1K/10K/100K; `test_long_replay_corpus.py` проверяет durable cursor,
  reasoning index, reducer, повторные cursor и исключение 100K архивных
  событий из model-visible context. `test_cross_device_chat_sync_js.py`
  проверяет 1K/10K/100K UI replay; opt-in
  `test_live_replay_browser_qa.py` прошёл в реальном Chromium на 10K и 100K:
  окно DOM ≤360 live-карточек, ручная прокрутка, старые события и reload.
  Production Safari на отдельном 6,002-строчном обезличенном чате сохранил
  последний диапазон после reload. Пункт №30 закрыт, но это не доказывает
  все шесть часов и не закрывает №50 или другие пункты.

- 2026-09-23 12:29 +05: пункт №10 подтверждён для технического экспорта.
  Owner-scoped `/api/chat/incident/{session_id}` отдаёт один детерминированный
  ZIP со схемой событий, версией, cursor/status, allowlisted причинами,
  числовыми TTFT/tool/compaction/reconnect метриками и синтетическим replay
  fixture. SQL-проекция выбирает только нужные JSON scalars (SQLite runtime,
  PostgreSQL SQL compile); полный `continuation`, context snapshot, prompts,
  receipts и аргументы инструментов не загружаются в архив. Отрицательные
  тесты с секретом в соседнем JSON-поле и чужим owner зелёные; полный pytest:
  7,145 passed, 21 skipped, 109 subtests. Изолированный Jetson-кандидат
  `d64b0a3` ответил 200 и выдал ZIP с одним синтетическим run и реальной
  TTFT; неизвестный session ID получил 404. Кнопка в отдельном тестовом
  браузерном чате вызвала тот же endpoint (200); адаптер браузера не отдал
  blob-download event, поэтому сохранение файла не заявляется отдельно.
  Пункт №10 закрыт как API/архив с UI invocation; production switch и общий
  шестичасовой gate остаются открытыми.

- 2026-09-23 05:58–06:05 +05: production log на `80364b0` зафиксировал
  `Server-side post-compaction plan recovery failed`: `save_plan` отклонил
  fallback, потому что он создавал draft с первым шагом `done` до Execute.
  Пользовательский скрин показал повторяющиеся остановки «Compaction
  succeeded, but the required fresh plan could not be rebuilt».
  Исправление `8825410`: recovery draft содержит только `pending`; Execute
  атомарно начинает первый шаг. Полный pytest: 7109 passed, 20 skipped,
  109 subtests. Изолированный Jetson candidate `:7131` исполнил тот же
  `save_plan → plan_action(execute)` на безопасном тестовом чате:
  `draft → executing`, шаги `in_progress/pending`; health 200, без traceback.
  Это адресное подтверждение failure mode; полный auto-compaction long-run и
  шестичасовой gate остаются открытыми. Основную пользовательскую Goal не
  запускали и её содержимое не читали.

- 2026-09-23 05:55–05:57 +05: `79c0134` исправляет порядок approval
  consume/DB commit. До правки одноразовый grant расходовался до записи
  решения, а ошибка БД оставляла неработающую pending-карточку. Теперь
  callback сохраняет точное решение под lock store, и лишь затем grant
  удаляется; ошибка записи возвращает 503 без запуска действия и сохраняет
  тот же pending ID для повтора. In-memory `ask_user` меняется только после
  commit. Негативные route/store тесты и полный pytest: 7108 passed,
  20 skipped, 109 subtests. На изолированном Jetson `:7131`, safe chat
  `1eaef620-7eb5-4e5c-b05f-beceb0ec5d92`, реальная qwen3.6 создала
  `PYTHON waiting`; Deny перевёл карточку в `failed`, reload сохранил тот же
  approval ID, команда не выполнялась, candidate logs без ERROR/Traceback.
  Кандидат остановлен; production `3971249` healthy. Это закрывает конкретный
  failure mode, но не весь №06/07 и не шестичасовой gate.

- 2026-09-23 05:39–05:41 +05: в `cf5c226` добавлен `history_revision` к
  дешёвому message-count probe; metadata-only Deny повышает ревизию сессии,
  а второй клиент точечно обновляет уже показанную карточку, не перерисовывая
  большой чат. На изолированном Jetson `:7131`, safe chat
  `0fcf9640-e1ee-402d-b7d0-dc188faa68fe`: две вкладки одновременно
  показывали тот же approval ID и `PYTHON waiting`; Deny во второй вкладке
  перевёл обе в `PYTHON failed` без reload, контекст остался 6.5%.
  Candidate logs без ERROR/Traceback; кандидат остановлен, production
  `c0f36f5` healthy. Адресные тесты: 51 passed. Пункт №50 остаётся открыт
  до полной матрицы reconnect/active tools/long-run и шестичасового gate.

- 2026-09-23 05:22–05:27 +05: после релиза `c0f36f5` обнаружено расхождение
  approval-карточки: при gated `python print(2+2)` live-клиент не создавал
  tool node, хотя reload показывал `PYTHON failed`. Причина: approval branch
  намеренно не посылает `tool_start`, а frontend создавал node только на
  `tool_start` и терял approval-only `tool_output`. Кандидат `205f991` на
  изолированном Jetson `:7131` с отдельной SQLite и реальной qwen3.6
  проверен в safe chat `cf85a63c-8a4c-47c5-a21f-b1f2d863cede`: до Deny
  live DOM имел `approval-pending`/`waiting` с точным approval_id; после
  Deny — `error`/`failed` с тем же ID; reload и вторая вкладка дали тот же
  результат. Команда не выполнялась. Полный локальный pytest: 7103 passed,
  20 skipped, 109 subtests; JS syntax/diff check зелёные. Кандидат остановлен,
  production healthy на `c0f36f5`. Пункт №50 и шестичасовой gate пока не
  закрыты: это адресный сценарий, не вся матрица live/replay.

- 2026-09-23 04:46–04:49 +05: isolated Jetson candidate `031ab98`, safe chat
  `9dc6295f-fe51-4af9-a275-4a8334107d99`. Goal создала draft Plan
  `0/2` и вызвала `ask_user` уже в попытке 1; второй клиент видел тот же
  waiting_user/7.5%, ответ `A` оставил ту же Goal active без ложной
  UI-плашки новой цели. До Execute оба `update_plan_step` и legacy
  `update_plan` получили отказ, Plan остался `0/2 draft`. После точного
  Execute сервер атомарно установил первый шаг `in_progress`, второй —
  `pending` (revision 2). Модель предложила ненужный `python`; approval gate
  заблокировал действие, я нажал Deny. После Deny Goal стала active без
  lease, последний run был done, но why-waiting оставался `queue/wait` более
  90 секунд — реальная потеря продолжения. Причина: deny-ветка возвращала
  control SSE сразу после `goal_action(resume)` и не вызывала серверный
  dispatch. Локально добавлен bounded denial guidance и dispatch того же
  goal_id/attempt; адресный route regression зелёный. Полный pytest:
  7102 passed, 20 skipped, 109 subtests. Первый live retry на новом образе
  ошибочно завершился `dispatch_failure` из-за конфигурации самого стенда:
  я выставил `APP_PORT=7131` внутри контейнера при Uvicorn на `7000`.
  Исправленный запуск подтвердил `internal_api_base()=127.0.0.1:7000`.
  Во втором safe chat `f2621c9e-d23b-41da-b2a6-35de6b208bf0` Python
  approval был отклонён; исходный run `b8674186...` был done, новый
  `3d89eccc...` стартовал автоматически с тем же Goal/attempt и завершился
  через `complete_goal` без повторного Python. `why-waiting` стал `idle`,
  Goal — `completed`, ошибок в логах кандидата нет. Это короткий smoke,
  не закрывает №03/04/46/51 и шестичасовой gate.

- 2026-09-23 04:33–04:35 +05: production safe chat
  `820e1405-1f54-4cd3-a353-46e933acc1d1` проверил Goal/Plan/question на
  реальной qwen3.6. Цель стартовала, но модель задала A/B обычным текстом;
  сервер начал следующую попытку без ответа. Legacy `update_plan` затем
  записал `2/2 done` в draft, хотя `update_plan_step` правильно отказал без
  Execute. Steering в активную Goal не поставил её на паузу: модель вызвала
  `ask_user`, сервер перешёл в `waiting_user`; Safari reload и второй клиент
  сохранили вопрос, статус и 12.0% контекста. Ответ `A` со второго клиента
  продолжил ту же серверную Goal и завершился через `complete_goal`;
  кратковременная UI-плашка второго клиента ошибочно показала новую Goal `A`
  на попытке 1. Локально: draft теперь не принимает непроверенный progress
  через legacy `update_plan`; UI preview не заменяет нетерминальную Goal;
  active-goal prompt требует `ask_user`, а не вопрос прозой. Адресные тесты:
  54 passed. Production ещё на старом образе; live повтор после релиза,
  полноценная Plan Execute-проверка и шестичасовой gate остаются открытыми.

- 2026-09-23 04:29 +05: isolated Jetson candidate `bd25553` запущен только
  на loopback `:7131` с отдельной SQLite, отдельным каталогом данных и
  реальной LM Studio `192.168.50.4:1234`; production `:5130` оставался
  healthy на `5a5dfdf`. В Safari safe chat
  `d769c2aa-c8f8-41ba-8ea0-3a09c0208c59` отправил безвредную арифметику,
  разрешил только точный `delegate_subagent` gate. Реальный child на
  `qwen3.8-27b-ultra-uncensored-heretic-native-mtp-preserved-nvfp4` вернул
  непустой видимый итог `4`; parent прочитал его и завершил сообщение.
  Safari reload сохранил 7.8%/10 264 токенов и карточки; второй browser
  показал тот же счётчик, финал, child status и detail `4`. В логах кандидата
  за smoke нет ERROR/Traceback; основной контейнер не перезапускался.
  На первом сравнении Chat→Agent инструментов я ошибочно заподозрил потерю
  состояния: режимы имеют раздельные предпочтения. Явно выключенные в Agent
  Web search и Shell access после reload остались off. Это не дефект.
  Этот короткий smoke не закрывает №45/50, долгие задачи и шестичасовой gate.

- 2026-09-23 04:09 +05: старт отдельного безопасного Safari-soak чата
  `820e1405-1f54-4cd3-a353-46e933acc1d1`; шестичасовая граница не ранее
  10:09 +05. На первом реальном round план создался, child стартовал и получил
  назначенную модель, но завершился с пустым `result` при наличии только
  thinking. Parent ошибочно трактовал `completed` как независимое подтверждение.
  Локальный runtime теперь переводит такой child в `failed`, а parent prompt
  требует непустой результат или явное evidence. Child system prompt отдельно
  требует видимый итог, а не только thinking/tool call. Regression для thinking-only
  stream прошёл. Повторный live smoke после релиза и остальная шестичасовая
  проверка открыты.
  В 04:17 Safari reload восстановил те же 7 видимых сообщений и 10.1% контекста.
  Второй уже авторизованный браузер открыл этот же safe chat без копирования
  cookies: 7 сообщений и 10.1% совпали; раскрытие первого старого thinking
  загрузило текст и 1.0s/100 tok. Это smoke старого production image, не
  доказательство исправления пустого child result после релиза.

- 2026-09-23: продолжение №03/42 (error/retry contract). `timeout` больше
  не наследует транспортный `retryable=true`: исход effectful действия
  неизвестен, и следующий шаг требует проверки receipt/состояния. Отрицательный
  тест и соседние MCP/host проверки: 41 passed, 12 subtests. Классификация
  provider ошибок, bounded retry budget и live smoke остаются открытыми.

- 2026-09-23: long-run SQLite incident для №03/06. На текущем production
  наблюдались `database is locked` при effect-intent, durable run checkpoint и
  Goal-failure записи. Помимо ожидающего релиза 30-секундного busy timeout,
  read-then-write транзакции теперь резервируют SQLite writer до первого
  SELECT через `BEGIN IMMEDIATE`; это исключает deadlock upgrade после
  чтения. Курсор и terminal timestamp в памяти подтверждаются только после
  успешного DB commit. Два конкурирующих writer и идемпотентная гонка
  одинаковых effect-intents покрыты отдельными тестами. Production smoke и
  полный recovery matrix остаются открытыми.

- 2026-09-23: продолжение №25 (качество summary). Ручной canonical `/compact`
  больше не записывает пустой checkpoint и возвращает 502 без изменения
  состояния, если reducer отдал пустоту либо эхо внутреннего context envelope.
  HTTP regression проверяет оба отказа; профильный набор — 31 passed.
  Полная проверка сохранения Goal/constraints/decisions/tool outcomes и live
  smoke остаются открытыми, поэтому №25 не закрыт.

- 2026-09-22: общий непроверенный batch после запроса пользователя сначала
  собрать изменения, затем тестировать: №08 per-run child budget, №42
  structured error normalization, №60 owner-visible capability inventory,
  №10 owner-scoped content-free ZIP incident export с allowlisted DB columns,
  агрегированными статусами/ошибками и детерминированной синтетической replay
  fixture; №09 частично — durable cursor lag, event-loop lag, process RSS и
  replay storage alert в admin diagnostics. Read-only replay больше не
  сканирует весь каталог артефактов при каждом открытии. Экспорт переведён
  на sync endpoint в threadpool и считает effect statuses за O(intents+runs),
  чтобы не блокировать event loop длинного чата. В меню чата добавлена
  owner-scoped выгрузка архива с понятной пометкой «без содержимого чата».
  Проверки, live smoke и release для этого
  batch ещё не выполнялись; пункты не считаются закрытыми.

- 2026-09-22: тот же непроверенный batch расширен для №08: добавлен
  `goal_max_wall_seconds` (0=без лимита), проверяемый перед новым round и
  каждым fallback transport POST. При достижении используется существующий
  durable `budget_exceeded → Goal waiting_user` путь с точным run ID;
  настройка и сообщение ожидания доступны в UI RU/EN. Лимит не прерывает
  уже начатое действие с неизвестным side effect, а ставит fence на следующей
  безопасной границе. Тесты написаны, но по просьбе пользователя пока не
  запускались; runtime-проверка ещё обязательна.

- 2026-09-22: №09 в том же batch получил content-free TTFT/tool latency
  счётчики из canonical run events, сохранение в durable run-state и
  агрегированные admin SLO alerts; backend-reported prefill TPS и child queue
  wait, SSE reconnect и compaction duration добавлены в метрики.
  Browser-local PerformanceObserver показывает предупреждение о длительных
  UI tasks только при активной Goal и отключается в скрытой вкладке.
  Метрики не читают тело ответа или output. Проверки и live доказательство
  ещё нужны; пункт не закрыт.

- 2026-09-22 ~20:23 +05: общий пакет №08/09/10/30/42/60 и replay-fix
  прошёл один интегрированный прогон: `7068 passed, 20 skipped, 9 warnings,
  109 subtests` за 180.54 секунд. Перед ним целевой набор дал 128 passed.
  Зафиксирован и устранён order-dependent тестовый дефект SQLite in-memory:
  `tests/test_agent_subagents.py` теперь создаёт собственную baseline schema
  перед модулем, а не полагается на импорт-time соединение. Python compile и
  изменённые JS syntax checks зелёные. Это не live acceptance и не выпуск.

- 2026-09-22 ~20:45 +05: изолированный localhost `:7130` (отдельная SQLite,
  не production) с реальной LM Studio `192.168.50.4:1234`. Safari и второй
  browser-клиент подключились к одному безопасному чату
  `b6853dc8-2bcc-4bcd-b0e4-c5226e4066ff`: после reload оба показали
  одинаковые 5.8% контекста и 0.2s/6 tok для первого thinking; безопасная
  Goal `2+2` завершилась через `complete_goal`, контекст вырос до 6.7%,
  финальный reload восстановил обе reasoning-карточки и tool result.
  Найдено ещё два дефекта: single-round history не использовал ленивый
  reasoning loader и потому терял stats; после патча Safari и второй
  браузер показали одинаковые 0.2s/6 tok. План из двух шагов: Execute
  атомарно сделал первый `in_progress`; модель завершила оба, но legacy
  `update_plan` оставил статус `2/2 executing`. Исправлено вычисление
  terminal status без смены stable IDs; focused tests прошли. Во время
  плана модель сообщила, что `update_plan_step` недоступен: route finetune
  ошибочно включал no-tool clamp для approved Plan. Исправлена доступность
  и добавлен regression `tool_inventory`; focused test прошёл. На повторном
  локальном run legacy `update_plan` перевёл Plan в `2/2 done` при тех же
  step IDs. Отдельная проверка replay выявила, что HTTP route до этого
  отбрасывал `tool_inventory` целиком; событие теперь проходит через route,
  durable run-state и replay, regression зелёный.
  Это короткие smoke, не шестичасовой acceptance; после последних патчей
  полный pytest и повторный live Plan ещё требуются.

- 2026-09-22: №15 обнаружен пропущенный legacy path. Failing-first тесты
  показали, что `mutation_paths(write_file, "path\\nbody")` возвращал `[]`;
  `hold([])` брал workspace sentinel, не совпадающий с path-lock fused
  операции. Поэтому обычная запись реально вклинивалась в окно
  mutation→verification и ломала проверку. Теперь parser зеркалит оба
  формата исполнителя (`path\\nbody` и JSON), raw `apply_patch` выделяет
  Add/Update/Delete пути. Concurrency regression с fused+legacy write и
  parser unit tests стали зелёными; полный `tests/test_action_fusion.py`:
  11 passed. Полный pytest после среза: 6993 passed, 20 skipped,
  109 subtests. Пункт остаётся открыт до проверки других write поверхностей,
  cross-host/path alias и live Agent smoke; файловый verifier для №07
  отложен до подтверждения общей очереди.

- 2026-09-22: №06/07 restart notification gap закрыт локально. Failing-first
  process-kill test показал, что `ChatToolIntent` после рестарта становился
  `unknown`, но `ChatWorkEvent` для второго клиента не появлялся. Теперь
  promotion intent→unknown и `effect_unknown` event записываются в той же
  SQLite transaction, что и `ChatRunState` running→interrupted, в обоих
  путях (с доступным replay и без него). Повторный startup идемпотентен,
  replay tool не происходит. Соседние 67 тестов passed; полный pytest на
  этом состоянии: 6991 passed, 20 skipped, 109 subtests; JS syntax,
  Python compile и diff-check clean. №07 всё ещё открыт: нет verifiable
  read-only check receipt и двух оставшихся пользовательских действий;
  six-hour safe Agent/Team soak и production release также не выполнялись.

- 2026-09-22: Goal Resume diagnostic follow-up. В production Safari
  (старый JS `20260921livefix17`) прочитан только технический статус:
  при переходах попыток 279→284 появляется `Goal changed; reload`, хотя
  Goal уже `active`. Локальный `chat-work.js` теперь на 409 перечитывает
  snapshot и не показывает ложную ошибку, если ровно та же Goal уже достигла
  желаемого статуса; Node VM проверил два клиента/статус. Боевой Goal с
  назначением вывода средств не возобновлялся агентом. На отдельной безопасной
  Safari-фикстуре Resume при недоступном dispatch вернул 503 (не 200),
  Goal сразу стала `waiting_user` с `dispatch_failure`. Два ложных локальных
  отказа были из-за harness: сперва отсутствовал `APP_PORT=7130` (внутренний
  POST ушёл на :7000), затем пустой fixture-чат не был загружен SessionManager
  после рестарта. С одним безвредным сообщением чат восстановился; модель
  фикстуры была выбрана фиктивная и UI её очистил, поэтому POST получил
  `No model selected`. Контроллер теперь распознаёт этот точный статический
  отказ и предлагает выбрать модель, не сохраняя произвольный detail.
  Это не доказывает причину ошибки на боевом Jetson: SSH не авторизован,
  технический `/why-waiting` там отвечает 404, а 192.168.50.4 недоступен
  с Mac. От пользователя запрошен уже видимый текст ошибки без повторного
  запуска. Тестовый сервер остановлен, вкладка закрыта, isolated SQLite
  снова в recoverable Trash. Полный pytest после основных исправлений:
  6989 passed, 20 skipped, 109 subtests; последний отдельный тест сохранения
  digest также прошёл (8 inbox tests). Новая финальная full-suite проверка
  после последнего микроизменения ещё нужна перед релизом.

- 2026-09-22: безопасный Safari smoke №07 на отдельном localhost `:7130`
  с отдельной SQLite и чатом `906c5056-0437-4852-a2a5-ddaa7c6b9403`.
  Реальная панель показала неизвестный `bash` intent с точными run/tool IDs;
  кнопки Resume до сверки не было. Нажатие `Do not retry` потребовало явное
  подтверждение, после него API `/unknown-effects` вернул пустой список,
  Goal осталась `waiting_user`, а Resume появилась — автоматического
  запуска не произошло. Тестовая вкладка закрыта, uvicorn остановлен,
  1.2 MiB временных тестовых данных перенесены в recoverable macOS Trash
  `/Users/xopmc/.Trash/odysseus-effects-smoke-20260922-1617`.
  Исправлен UX-хвост: пустой инбокс теперь даёт `resume_goal` в панели
  ожидания. Полный pytest до последнего UX-среза: 6987 passed, 20 skipped,
  109 subtests; профильные проверки последнего среза: 27 passed.
  Production Safari всё ещё на старом JS `20260921livefix17`; там при
  переходах Goal observed `Goal changed; reload` и попытки 279→281.
  Локальный UI теперь перечитывает CAS state и подавляет 409 только когда
  ровно та же Goal уже достигла желаемого состояния; Node VM regression
  зелёный. Основной Goal не нажимался агентом. Host `192.168.50.4`
  недоступен с Mac (ping `Host is down`, порт 1234 timeout), но причина
  server-side модельного сбоя на Jetson без его логов не доказана.

- 2026-09-22: продолжение №07: `unknown` теперь блокирует server-owned
  Resume/Revise/lease/dispatch до явной сверки. Появились owner-scoped
  `no-retry` CAS endpoint, серверный decision receipt (не доказательство
  внешнего эффекта), SSE work-events и инбокс в панели ожидания. Два клиента
  обновляют его по событию; никаких автоматических повторов/Resume после
  решения. Failing-first тесты проверяют запрет Resume, каскад из >200
  intent rows, owner/CAS, порядок и UI-блокировку. Пункт №07 всё ещё открыт:
  нужны два оставшихся действия с настоящими verification receipts,
  полноценный live smoke и сохранение unknown events при process recovery.
  Отдельно локально исправлен ложный успех при ручном Goal Resume: если
  controller получил HTTP-ошибку, Goal сразу ждёт пользователя с явной
  причиной `dispatch_failure`, а API возвращает 503 вместо active/success.
  На боевом Safari Goal на паузе, но API `why-waiting` той старой версии
  отвечает 404, SSH отклоняет ключ; точную причину видимой пользователю
  ошибки без содержимого модели подтвердить пока не удалось. Опасную
  основную цель не запускать ради диагностики.

- 2026-09-22: №07 частично реализован. Добавлена owner-scoped таблица
  `chat_tool_intents` без сохранения аргументов действия, durable intent
  перед dispatch эффектного Agent-инструмента, результат/unknown receipt и
  точная привязка к run/tool-call. Restart переводит незакрытые intents в
  `unknown` в одной DB-транзакции с `running→interrupted`. Новый эффект
  не запускается при наличии неизвестного исхода, Goal переходит в
  `waiting_user`, а startup recovery не возобновляет его автоматически.
  Read-only `/api/chat/work/{session}/unknown-effects` возвращает одинаковый
  owner-scoped snapshot двум клиентам. Проверены отказ чужому owner,
  отсутствие тела действия в ответе, CAS-инвариант, process-kill и порядок
  `intent→dispatch→receipt`; полный pytest: 6979 passed, 20 skipped,
  109 subtests. Пункт остаётся открыт: нет UI трёх действий, проверяемых
  внешних receipts для произвольных эффектов и live smoke на новом чате.

- 2026-09-22: №06 process-kill recovery расширен. Новый тест запускает
  отдельный Python web-run worker с отдельными SQLite/replay, создаёт
  `agent_runs.start`, затем делает `os._exit(17)` ровно после `tool_start`.
  Failing-first подтвердил: replay-файл имел seq=1, но run-state DB оставалась
  на seq=0. Теперь `tool_start` синхронно создаёт durable checkpoint до
  продолжения generator к потенциальному действию. Второй parameterized
  process-kill тест поймал такой же разрыв для `context_compaction_failed`
  и `agent_terminal`; оба события также стали checkpoint boundaries.
  Отдельный процесс восстановил interrupted run, точный cursor/replay и одно
  assistant-сообщение; второй recovery идемпотентен, tool не исполняется
  снова. Профильные 20 тестов passed. Это не вся матрица: атомарность
  Goal failure row с terminal event, реальные effect receipts, child join и
  power-loss fsync ещё требуют отдельной проверки.
  Новый checkpoint на `tool_start` не должен превращаться в O(number of prior
  runs) DB scan на каждом инструменте: failing-first SQL assertion показал
  два `SELECT chat_run_states` даже при неизменном context snapshot. Теперь
  текущая row читается первой, а прошлые runs сверяются только при реально
  новом measurement; unchanged tool boundary делает один SELECT. High-water
  regression для нового run и process-kill checks остаются зелёными (21
  профильный тест passed). Полный pytest на этом срезе: 6971 passed,
  20 skipped, 109 subtests; syntax и diff-check clean.

- 2026-09-22: №06 durable restart slice. Failing-first SQLite тест поймал
  неверный `next_seq=0` для единственного уже записанного события (`last_seq=0`)
  после restart; snapshot теперь возвращает 1. Реалистичный ReplayLog+SQLite
  тест проверил `running→interrupted`, `terminal_reason=process_restarted`,
  сохранение `context_revision`/durable cursor, одно каноническое assistant
  сообщение и идемпотентность повторного recovery без re-execution tool.
  Второй failing-first тест выявил откат `durable_seq` с 0 на −1 через
  `row.durable_seq or -1` при non-durable checkpoint; теперь курсор монотонен.
  Первый полный pytest выявил загрязнение тестового процесса: старый suite
  оставлял подменённую `core.database.SessionLocal`, так что 3 новых теста
  читали другую базу. Тесты теперь явно возвращают каноническую фабрику и
  проверяют реальную запись `ledger_hash`, исключая ложный pass.
  Профильный прогон: 51 passed; повторный полный pytest после исправления
  фикстуры: 6967 passed, 20 skipped, 109 subtests; Python compile,
  JS syntax и `git diff --check` clean. Это только две точки recovery matrix,
  остальные kill/restart boundaries и subprocess-level test ещё открыты.

- 2026-09-22: №03/№05 bounded Goal retry. Failing-first тест показал, что
  `keep_active=True` после шести одинаковых ошибок checkpoint всё ещё держал
  Goal активной; флаг и бесконечный обход лимита удалены. Три неуспешных
  attempt переводят Goal в `waiting_user`, сохраняют прежний `ledger_hash`
  и выставляют `wait_reason=context_compaction`. Панель «Почему агент ждёт?»
  предлагает проверить summarizer/policy и явно возобновить Goal после
  исправления, не ищет несуществующий вопрос. Второй failing-first тест
  поймал сброс `failure_count` при чередовании HTTP 503, timeout и HTTP 502;
  теперь число последовательных неудач растёт независимо от текста ошибки
  и сбрасывается только при зафиксированном прогрессе/восстановлении.
  Профильные тесты (SQLite, reducer и реальный JS-модуль) прошли. Первый
  полный pytest во время правки одного source-assert дал 1 падение;
  повторный на зафиксированном состоянии: 6963 passed, 20 skipped,
  109 subtests. JS syntax, py_compile и `git diff --check` clean.
  Live compaction smoke и длительная проверка ещё открыты.

- 2026-09-22 ~10:07 +05: реальный Safari smoke №01/№03/№05 выявил
  критический баг zero-token provider failure: локальная безопасная Goal с
  недоступным endpoint `127.0.0.1:1` за несколько секунд дошла до attempt
  27 при `failure_count=0`. Сервер изолированного `:7130` остановлен;
  SQLite показал только `goal_attempt_started`, ни одного
  `goal_attempt_failed`. Причина: direct low-signal и обычный Agent loop
  выдавали `agent_terminal` при provider error только если уже успели
  получить текст/мышление/tool event. На первом error Goal controller видел
  успешное окончание, запускал следующий attempt без backoff. Теперь оба
  пути всегда отдают failed terminal (без выдуманного usage при пустом
  ответе), поэтому failure_count достигает 3 и Goal переходит в
  `waiting_user` с причиной `provider_failure`. В Safari отдельный тестовый
  чат `b3c7ea66-c06f-4373-b132-0ca40cee24e6` показал phase=user,
  attempt=30, точный run/checkpoint и предупреждение проверить endpoint
  перед Resume. Reload Safari сохранил то же состояние; второй браузер
  (IAB) показал тот же run ID, checkpoint и действие. Профильные 145 тестов
  passed, включая failing-first first-round errors, SQLite и JS.
  Тестовые вкладки закрыты, исходный Safari чат не изменён, локальный
  сервер остановлен, изолированные данные перемещены в macOS Trash по
  адресу `odysseus-goal-wait-smoke.ZxmQWa-20260922` (восстановимы).
  Полный pytest после среза: 6961 passed, 20 skipped, 109 subtests;
  JS syntax, py_compile и diff-check clean. Шестичасовой безопасный
  Agent/Team soak и остальные TODO всё ещё нужны.

- 2026-09-22: №05/№01 Goal monologue recovery. Реальный Agent-loop test
  подтвердил, что шесть одинаковых ответов без tool action раньше переводили
  Goal в `waiting_user` без вопроса. Теперь перед остановкой attempt выходит
  `loop_breaker_triggered` с явной причиной `repeated_premature_stop`;
  `ChatWorkStore` сохраняет отдельный owner-scoped `_wait_reason`, который
  очищается при Resume. Панель «Почему агент ждёт?» отличает эту ситуацию
  от настоящего `ask_user`/approval, показывает объяснение и кнопку Resume,
  которая вызывает существующий серверный Goal action. Проверены SQLite
  owner isolation, оба типа ожидания и реальный JS-модуль; PWA URLs и RU
  перевод согласованы. Полный pytest: 6959 passed, 20 skipped, 109 subtests;
  после добавленной проверки `ask_user` 4 профильных теста passed, JS syntax,
  py_compile и `git diff --check` clean. Реальный Safari smoke и долгий
  Agent/Team soak всё ещё нужны, поэтому пункты остаются открыты.

- 2026-09-22: №05 начат. Новый `LoopDetector` сравнивает хэши полных
  action→observation batch после tool result, игнорируя только изменчивую
  длительность. Три одинаковых наблюдения или A↔B-цикл вызывают одно
  диагностическое предупреждение; продолжение без новых данных приводит к
  bounded tool-free остановке, а изменившееся наблюдение сбрасывает warning.
  Failing-first unit и реальный `stream_agent_loop` regression покрывают
  повторное действие даже при поясняющем тексте модели, цикл, ошибку с
  меняющимся временем и отсутствие ложной тревоги при новых данных или
  разных timestamp в аргументах действия. Код не
  сохраняет содержимое результатов в состоянии детектора и не включает его
  в публичные события. Полный pytest: 6951 passed, 20 skipped,
  109 subtests; после уточнения нормализации аргументов 18 профильных
  тестов passed, `git diff --check` и py_compile clean. Монолог, no-op
  compaction и live smoke пока открыты.

- 2026-09-22: №05 no-op compaction срез. Failing-first тест подтвердил, что
  `shape_request` при достигнутом пороге принимал `unchanged` и отправлял
  неизменённый контекст в модель; теперь это явный failed checkpoint без
  model dispatch. Agent integration test проверил терминальное событие.
  Второй тест выявил, что после no-op экономического компактора аварийный
  fallback вызывал тот же компактор ещё раз в одном round; теперь он
  не повторяется. Ниже safety trigger экономическая попытка откладывается
  с `native_no_reduction`, выше — останавливается как `uncompactable` без
  потери checkpoint. Профильные 44 теста/8 subtests пройдены; полный pytest:
  6956 passed, 20 skipped, 109 subtests; `git diff --check` clean.
  Live smoke и проверка долгого monologue ещё требуются.

- 2026-09-22 ~09:13 +05: №01 начат. Серверный `RunWaitTracker` сохраняет
  красноречивую, но content-free фазу model/tool/approval/user и фактические
  model/endpoint/tool IDs; terminal restart превращается в reconnect.
  Owner-scoped `/api/chat/work/{session}/why-waiting` объединяет точный run,
  Goal lease (без token/objective), bounded active-child summary (без objective
  и assigned context), длительность и durable checkpoint. Возвращает только
  безопасные action codes `answer/resume_goal/reconnect/inspect/wait/none`.
  Failing-first тесты проверили фазовые переходы, approval против терминального
  run, queue с удерживаемым lease, interrupted/reconnect, чужого owner,
  двух клиентов одного owner и отсутствие секрета в JSON.
  UI получил компактную плавающую кнопку `?` рядом с Plan, прокручиваемую
  панель и явные действия; при отсутствии карточки вопроса больше нет
  молчаливого no-op. Node VM тест реального `chat-work.js` проверил фазу,
  IDs, ответ, reconnect fallback, паузу и stale cross-chat result.
  PWA URLs обновлены. Полный pytest и Safari/mobile live smoke на этом
  срезе: 6943 passed, 20 skipped, 109 subtests; JS syntax и diff-check clean.
  Failing-first тест выявил ложную длительность paused Goal от предыдущего
  model call; `status_since` теперь берётся из durable Goal и пауза показывает
  реальное время в этом статусе. В отдельном localhost `:7130` с отдельными
  SQLite и `ODYSSEUS_DATA_DIR` Safari и IAB подключились к одному безопасному
  fixture-чату `95e44659-33eb-4ec5-84c9-986d11cb0d24`: обе панели показали
  одинаковые run `bbbb…`, child `fixture-child`, worker-model, endpoint,
  `run_tests`, lease и checkpoint `seq 7/rev 3`; после reload обе восстановили
  панель, IAB подтвердил auto-collapse. Тестовые вкладки закрыты, исходная
  Safari вкладка осталась открытой; сервер остановлен, изолированные данные
  перемещены в macOS Trash. Это synthetic-state UI smoke, не боевой Agent/Team
  и не mobile 390×844 (viewport capability второго браузера недоступна).
  Полный pytest после status_since патча: 6944 passed, 20 skipped,
  109 subtests; JS syntax и diff-check clean. Team phase, unknown
  effect и все recovery пути не закрыты, поэтому пункт открыт.

- 2026-09-23: №01 дополнен regression для stalled model request: два
  owner-клиента видят одинаковую фазу, длительность, lease, модель, endpoint,
  child ID, durable checkpoint и безопасное действие `inspect`; чужой owner
  получает 403, assigned context отсутствует в ответе. Отдельный opt-in
  localhost Playwright/Chromium smoke (desktop 1280×800 и mobile 390×844)
  раскрыл панель в двух независимых клиентах, сверил поля после reload,
  отсутствие page/console ошибок и отсутствие обрезания на мобильной ширине.
  Реальный зависший provider request и Team-specific phase ещё не проверены;
  пункт остаётся открытым.
  Затем browser fixture усилена: вместо подмены `/why-waiting` два клиента
  читают настоящий маршрут для server-owned detached run, заблокированного
  на безопасной model-like границе без provider I/O. Повторный opt-in
  Chromium desktop/mobile smoke после reload прошёл. Настоящий зависший
  provider и Team phase по-прежнему открыты.
  Усиленный reload-тест затем выявил реальный startup race: отложенное на
  0,5 с восстановление ошибочно объявляло новый run этого же процесса
  прерванным и ставило `[Cancelled by user]` на последнее сохранённое
  сообщение, хотя detached run продолжал `running`. Recovery теперь
  ограничено состояниями, существовавшими до process-start cutoff, и
  дополнительно пропускает run с совпадающим активным in-memory ID.
  Failing-first browser regression с настоящим durable event и unit-тест
  обоих fences прошли. Production release/smoke ещё требуются.

- 2026-09-22 ~08:40 +05: №04 начат. `ProgressTracker` отделяет транспортный
  heartbeat и обычные SSE tokens/round/tool_progress от доказанных изменений.
  Только уникальная revision рабочего Plan, nonempty diff, artifact ID/version
  или уникальный завершённый test/lint/hash result повышает progress revision.
  Повтор идентичного evidence не считается новым продвижением; test failure
  считается evidence, но failed patch — нет. Run snapshot/event page показывают
  activity/heartbeat/progress timestamps, elapsed и `stalled` после 600 с;
  terminal/recovered run не помечается stalled. Метаданные сохраняются в
  существующем durable run continuation без содержимого чата. Failing-first
  тесты, реальный SSE heartbeat path и отдельная SQLite: 5 passed. Полный
  pytest после первого среза: 6932 passed, 20 skipped, 109 subtests.
  Дополнительный failing-first тест выявил ложный прогресс от простого
  переименования Plan с новой revision; marker теперь зависит от статусов
  шагов, а не title/revision. Durable lookup через `describe_run` проверен.
  Отдельный тест обнаружил отсутствие учёта Goal checkpoint evidence;
  учитываются только непустые progress+checkpoint активной/завершённой цели,
  без повторного счёта при простом повышении revision или waiting_user.
  Полный pytest после последнего Goal marker среза: 6932 passed, 20 skipped,
  109 subtests. Следующий UI-срез добавил компактный Goal warning по
  owner-scoped `/api/chat/run/{session}` snapshot, 15-секундный visible-only
  poll и различение живого heartbeat от подтверждённого прогресса. Warning
  исчезает на pause/done и не переживает переход в другой чат; Node VM
  прогон реального `chat-work.js` проверил оба случая и late-response race.
  PWA URLs/Service Worker cache согласованы. 40 профильных тестов passed;
  локальный полный pytest на UI-срезе: 6935 passed, 20 skipped, 109 subtests;
  JS syntax и `git diff --check` clean. На реальном Safari открыт отдельный
  localhost `:7130` с отдельными SQLite и `ODYSSEUS_DATA_DIR`. Для безопасного
  фикстурного чата `c5b65be8-99f3-42ea-8ec5-b74e30a4a9a1` показано
  предупреждение о 12 мин без прогресса; раскрытие Goal показало detail,
  после перевода фикстуры в paused и reload предупреждение исчезло.
  Тестовая вкладка закрыта, исходная Safari вкладка осталась открытой;
  локальный сервер штатно остановлен, production/Jetson не изменены.
  Изолированные тестовые данные перенесены в macOS Trash, откуда их можно
  восстановить; дополнительный чат не остался в рабочем сервисе.
  Это UI smoke с синтетическим состоянием, не реальный long-run Agent/Team.
  Mobile/Jetson smoke ещё нужны.
  Фактическое автоматическое действие watchdog пока не реализовано.
  UI alert, Goal/Team integration и live smoke ещё не выполнены; пункт открыт.

- 2026-09-22 ~08:11 +05: шестичасовой технический Safari soak завершился:
  710 выборок за интервал от 2026-09-21 21:11 до 2026-09-22 03:10 UTC;
  CPU Safari в выборках 0–7.8% (среднее 1.66%), RSS 370496–372576 KiB
  (среднее 371680 KiB). `goal=active` в 296 выборках,
  `goal=waiting_user` в 414; значит это **не** доказательство шести часов
  непрерывной активной агентской работы. В конце run `done`, live и durable
  cursor равны 66660, context revision 6667; Safari после reload восстановил
  Goal, Plan, composer и context pill 62%. Production Goal не возобновляли:
  обнаруженное содержание задачи содержит недопустимое вредоносное действие.
  Для полного acceptance нужен отдельный безопасный долгий Agent/Team smoke.

- 2026-09-22 ~08:00 +05: №03 начат с конкретного retry-дефекта.
  Failing-first регрессия доказала три повторных POST после `ReadTimeout`,
  `WriteTimeout` и `RemoteProtocolError` при `max_retries=3`. После исправления
  неоднозначная доставка не повторяется и запрещает fallback; HTTP detail
  не раскрывает endpoint. `tests/test_llm_core_fallback.py`: 80 passed.
  Полный pytest на текущем коде: 6903 passed, 20 skipped, 109 subtests;
  `git diff --check` clean. Остальные категории, jitter/budget и live smoke
  не проверены, пункт открыт.
  Дополнительная failing-first проверка выявила, что background utility fallback
  игнорировал `fallback_eligible=False` и отправлял тот же запрос другой модели
  после неизвестного исхода. Sync/async utility chains теперь соблюдают запрет;
  профильный suite: 81 passed. Полный pytest после этого среза: 6904 passed,
  20 skipped, 109 subtests. Safari reload основного чата восстановил
  технические Goal/Plan и context 62% после короткой загрузочной фазы;
  активную Goal не возобновляли, поскольку содержание задачи нельзя безопасно
  выполнять. На момент этого среза шестичасовой мониторинг ещё шёл.
  Следующий failing-first срез обнаружил тройные повторы на upstream HTTP
  `502/504`. Их outcome признан неоднозначным и fallback запрещён. Повтор
  допустим только для явных `429/503` или ошибок до отправки запроса, с
  ограничением трёх попыток и 10 секунд, jitter и соблюдением `Retry-After`.
  Тесты включают 429→200 через два вызова и отклонение задержки за пределами
  бюджета; профильный suite: 86 passed. Полный pytest на этом срезе:
  6909 passed, 20 skipped, 109 subtests. Streaming negative tests затем
  обнаружили небезопасный fallback после `ReadTimeout` и HTTP 504. Теперь
  четыре provider stream path помечают post-dispatch ReadTimeout как
  `fallback_eligible=false`, а HTTP-response fallback допускается только для
  явных 429/503. Профильный suite: 90 passed; полный pytest: 6913 passed,
  20 skipped, 109 subtests. Последний срез добавил стабильные категории
  `rate_limit`, `provider_unload`, `schema_mismatch`, `context`, `timeout`,
  `transport`, `unknown_outcome` в non-stream HTTP exceptions и HTTP/timeout
  stream events. Failing-first тесты подтвердили отсутствие этих полей;
  профильный suite: 101 passed; полный pytest: 6924 passed, 20 skipped,
  109 subtests. Legacy sync `llm_call_with_fallback` также воспроизвёл replay
  после `ReadTimeout` и получил ту же консервативную классификацию без raw URL
  и неизвестного provider error body; профильный suite: 102 passed. Остальные
  provider event shapes, Agent/Team propagation и live smoke пока не закрыты.
  Полный pytest после sync-пути: 6925 passed, 20 skipped, 109 subtests.
  Failing-first тесты для provider `error` внутри HTTP 200 обнаружили
  резервное переключение на `schema_mismatch`, а malformed success раскрывал
  provider body. Теперь этот путь классифицируется и не повторяется, а
  malformed schema возвращает безопасный текст; профильный suite: 104 passed,
  полный pytest: 6927 passed, 20 skipped, 109 subtests.

- 2026-09-22 ~07:48 +05: №42 начат: общий `enrich_tool_error` добавляет
  `error_category`, `next_action` и conservative `retryable` для
  `not_found`, `permission_denied`, `transport_unavailable`, `stale_revision`,
  `unknown_outcome`, `timeout`; legacy `code/error` остаются неизменны.
  Agent wrapper покрывает и ранние policy denials; Team сохраняет категорию
  в durable tool intent/result. MCP получил явные transport/unknown codes.
  Позитивный/отрицательный тест подтвердил, что потерянный SSH reply после
  effectful host call = `unknown_outcome`, один вызов без replay и без
  деталей stderr. Узкие suites: 157 Agent+policy, 24 Team, 29 host/MCP
  passed. Полный pytest и live Agent smoke ещё выполняются; пункт открыт.
  Первый полный pytest после wrapper: 6899 passed, 20 skipped, 109 subtests.
  Failing-first тест показал, что отсутствующий `read_file` ещё
  классифицировался как generic failed и раскрывал абсолютный путь. Возвраты
  `not_found`/`permission_denied`/`stale_revision` теперь имеют точный code и
  безопасный текст без host path. В полностью изолированном live Agent-чате
  модель действительно вызвала `read_file` для отсутствующего fixture,
  persisted event имел exit_code=1, а её ответ получил category `not_found`
  и предписанный next action. Чат удалён. Повторный полный pytest после этого
  среза требуется; категория покрывает ещё не все legacy tools.
  Повторный полный pytest после read_file классификации: 6900 passed,
  20 skipped, 109 subtests. Пункт остаётся частичным до выравнивания
  оставшихся legacy/approval ошибок и Jetson release smoke.

- 2026-09-22 ~07:26 +05: №41 частично реализован: `inspect_toolchain` выдаёт
  версии Python, фиксированных Node/Git/compiler/LSP/container CLI и
  фиксированного набора Python packages. Он не принимает произвольную
  программу/URL и не ищет бинарники по рабочему `PATH`; stdout/version
  ограничены 1 KiB, subprocess — 2 с. Без endpoint_id сеть не трогает;
  при явном owner-visible endpoint_id использует существующий HEAD-only
  `http_probe`. Jetson host helper получил отдельную фиксированную диагностику
  без local fallback; endpoint probe на этом маршруте явно unavailable,
  поскольку он выполняется на web-хосте. Отрицательные тесты покрывают
  workspace shim `git`, произвольный URL/путь, owner forwarding и host route.
  Узкий прогон: 14 passed. Live Agent/Jetson и полный pytest ещё нужны;
  пункт открыт.
  Полный локальный pytest после host adapter: 6893 passed, 20 skipped,
  109 subtests. В полностью изолированном `:7130` Agent на qwen3.6-35b
  реально вызвал `inspect_toolchain` с зарегистрированным endpoint_id;
  persisted tool event `exit_code=0`, endpoint HEAD ответил HTTP 200
  (~9 ms). Тестовый чат удалён. Jetson release и Team surface для этого
  инструмента ещё не проверены, пункт остаётся открыт.
  Team surface теперь также показывает `inspect_toolchain` read-only reviewer,
  но не `bash`; `file.call` использует тот же fixed host_files helper, что
  Agent SSH. Host/Team suites: 73 passed, 58 subtests; полный pytest идёт.
  Реальный Safari проверен через accessibility API без чтения текста задачи:
  Plan-кнопка раскрывается/закрывается, панель Subagents раскрывается/закрывается,
  UI показывает 3 completed и 4 failed — ровно серверный snapshot; Goal
  остаётся `waiting_user`, context pill 62%. Это проверка старого production
  SHA, не acceptance нового кода.
  Полный pytest после Team wiring: 6895 passed, 20 skipped, 109 subtests.

- 2026-09-22 ~07:10 +05: №40 частично реализован локально: `compare_files`
  вычисляет SHA-256 двух разрешённых файлов, exact/normalized-newline equality
  и ограниченный unified diff; `verify_hashes` проверяет до 16 заявленных
  SHA-256 без отправки содержимого. Чтение ограничено 64 MiB на файл, с
  fstat до/после; binary не попадает в diff, invalid/sensitive/out-of-workspace
  пути отклоняются. Host-bound маршрут явно отвечает `not_supported_by_route`
  без silent local fallback. Изолированный live Agent на qwen3.6-35b
  действительно вызвал оба инструмента. Первые запросы модели содержали
  выдуманный `/Users/x/...` и получили `exit_code=1`; после `get_workspace`
  повторные `compare_files` и `verify_hashes` сохранились с `exit_code=0`.
  Тестовый чат удалён. Узкие suites: 154 passed, 49 subtests. Полный pytest,
  host adapter и Jetson/release smoke ещё нужны; чекбокс открыт. Полный pytest
  на этом срезе: 6884 passed, 20 skipped, 109 subtests.
  Следующий срез добавил фиксированный Jetson host helper для обоих tools
  вместо `not_supported_by_route`: чтение остаётся read-only, 2 MiB на файл,
  точные SHA-256 и bounded diff; Team/model path guard проверяет каждый
  `before`/`after` и каждый элемент `verify_hashes`. Отрицательные тесты
  закрывают `.env`, ложный hash и отсутствие local fallback в host-bound
  dispatch. Host suites: 58 passed, 9 subtests. Реальный Jetson deploy/smoke
  и полный pytest после host adapter ещё не выполнены.
  Полный pytest после host adapter: 6886 passed, 20 skipped, 109 subtests.
  Team registry теперь показывает оба read-only инструмента reviewer/researcher
  при доверенном host profile; write_file им по-прежнему запрещён (отдельный
  тест прошёл). Production Safari перезагружен read-only в ~07:19 +05;
  следующий технический snapshot сохранил те же Goal/Plan/run cursors.

- 2026-09-22 ~06:23 +05: №39 начат: ObservationPack получил индекс
  артефактов по точному `run_id` внутри owner/session, ограниченный буквальный
  `search_artifacts` (до 20 коротких совпадений и 32 MiB сканирования за
  страницу) с opaque cursor и ID для `read_tool_artifact`. Старые артефакты
  без достоверного run-id остаются доступными по известному ID, но в поиске
  не выдаются под выдуманным run. В Agent dispatch передаётся серверный
  run-id; индексируются большие tool results и read_file/test artifacts.
  Отрицательные тесты покрывают чужого owner/chat/run, symlink, неверные
  аргументы и возобновление после scan budget. Узкий прогон: 49 passed,
  49 subtests; широкий срез до последней метки `run_id` — 6870 passed,
  20 skipped. Live Agent smoke и Jetson/release проверки не проведены;
  чекбокс остаётся открытым.
  Дополнительная проверка обнаружила обход через symlink на родительском
  `runs/` (первоначальный тест упал); теперь поиск и recall отклоняют такие
  каталоги, повторный узкий прогон ObservationPack: 13 passed. Полный pytest
  после исправления: 6871 passed, 20 skipped, 109 subtests.
  Следующий срез устранил известный-ID bypass: новые индексированные артефакты
  помечаются `runbound`, а `read_tool_artifact` требует совпадения текущего
  серверного run-id. Legacy-артефакты без индекса сохраняют прежнее чтение;
  проверены отрицательные случаи другого run, owner и отсутствующего run.
  Host read/test archives теперь также получают run-id. Затронутые suites:
  52 passed; полный pytest после этого среза: 6873 passed, 20 skipped,
  109 subtests. Дополнительный отрицательный тест обнаружил symlink-обход
  через каталог owner (до фикса упал); `recall` и `search` теперь отклоняют
  его, затронутые suites: 53 passed. Повторный полный прогон нужен.
  Повторный полный pytest на итоговом коде этого среза: 6874 passed,
  20 skipped, 109 subtests. Изолированный live Agent smoke всё ещё нужен.
  Live Agent на изолированном `:7130` с qwen3.6-35b обнаружил два реальных
  дефекта: без ChromaDB явное `search_artifacts` выпадало из low-signal
  Terminus выбора; `read_file` через MCP fallback терял server-owned run-id,
  из-за чего созданный artifact не попадал в индекс текущего run.
  Оба исправлены с failing-first регрессионными тестами. Повторный реальный
  Agent run выполнил `read_file → search_artifacts → read_tool_artifact`:
  persisted tool events показывают три `exit_code=0`, поиск вернул ровно одно
  совпадение; все шесть созданных мной smoke-чатов удалены. Первый тестовый
  запуск по ошибке использовал общий каталог `data/` при отдельной SQLite;
  его чаты и observations удалены по точным session IDs. Повторный запуск
  использовал отдельные SQLite и `ODYSSEUS_DATA_DIR` под `/tmp`.
  Full pytest после этих исправлений ещё нужен; Jetson не менялся.
  Полный прогон сначала показал 4 падения, не связанных с кодом: первый
  тестовый запуск с общим каталогом `data/` автоматически создал skill
  `read-file-then-search-and-fetch-artifact` с владельцем `smoke_admin`.
  Его injection выставлял external-untrusted gate в старых тестах. Точный
  созданный нами skill перенесён в `/tmp/odysseus-artifact-smoke.ZWyst0/`
  (восстановим); другие файлы `data/skills` не тронуты. Все 4 теста после
  очистки проходят. Правило для следующих smoke: задавать и отдельный
  `DATABASE_URL`, и `ODYSSEUS_DATA_DIR`, а не только SQLite.
  Финальный полный pytest после root-cause fixes: 6877 passed, 20 skipped,
  109 subtests. Изолированный live run сохранил три успешных tool events;
  активная production Goal/Jetson release acceptance остаются открытыми.

- 2026-09-22 ~06:05 +05: №38 частично реализован: установленный Playwright
  MCP на изолированном `:7130` отдаёт 30 browser tools; 25 reviewed
  navigation/snapshot/click/type/screenshot/console/network tools теперь
  классифицированы серверной политикой. Пять опасных методов
  (`browser_run_code_unsafe`, `browser_evaluate`, `browser_file_upload`,
  `browser_drop`, `browser_network_request`) скрыты из схем и отклоняются
  непосредственно перед dispatch. Отдельный `can_use_browser` проверяется
  перед каждым вызовом; отрицательный тест доказывает отзыв доступа и запрет
  unsafe-метода даже при разрешённом браузере. Узкий прогон: 185 passed,
  49 subtests; полный pytest: 6863 passed, 20 skipped, 109 subtests.
  Отдельный live MCP smoke на `data:` fixture подтвердил navigation,
  accessibility snapshot, type, click, screenshot, console и network-error
  tools. Неверный первый вызов `type/click` с устаревшим полем `ref` вернул
  ошибку схемы; вызов по актуальному `target` прошёл, это полезный regression
  пример для tool-schema versioning. Запись короткого redacted сценария и
  live Agent smoke ещё не сделаны, поэтому пункт остаётся открытым.

- 2026-09-22 ~05:53 +05: №37 частично реализован: `http_probe` принимает
  только owner-visible enabled `ModelEndpoint.id` и отправляет один HEAD `/`
  без auth/body/proxy/redirect; DNS-адрес закреплён на socket connect, TLS
  проверяется стандартным trust store, ответ ограничен разрешёнными headers.
  Отклоняются userinfo/query/fragment, link-local/reserved и смешанный DNS;
  тесты покрывают owner isolation, TLS failure, IP fallback, отсутствие POST,
  редиректа и cookie leak. Реальный зарегистрированный LM Studio
  `192.168.50.4:1234` ответил HTTP 200; безопасный локальный Agent вызвал
  инструмент после approval, desktop/mobile reload показали одну `done`
  карточку и HTTP 200 без page errors. Полный pytest: 6860 passed,
  20 skipped, 105 subtests. Тестовый чат удалён. Jetson/release
  smoke и более широкие типы зарегистрированных targets ещё не проверены,
  поэтому чекбокс остаётся открытым.

- 2026-09-22 ~05:41 +05: №36 частично реализован в фиксированном host helper:
  `inspect_process` читает только процесс фиксированного Unix-пользователя и
  возвращает PID/start ticks без argv/env; `inspect_port` требует тот же
  process fence и сверяет inode TCP LISTEN с fd процесса; `tail_log` требует
  process fence, владелец файла и canonical путь под host workspace, читает
  не более 64 KiB/200 строк и показывает максимум 16 KiB. Отрицательные
  fixture-тесты покрывают PID reuse, другого Unix-пользователя, отсутствие
  fence, неверный порт, unreadable socket fd, symlink и выход за workspace.
  Полный pytest на этом срезе: 6851 passed, 20 skipped, 105 subtests. В безопасном локальном
  Agent-чате инструмент был выбран и после approval честно ответил
  `not_supported_by_route`, поскольку host mode выключен; чат удалён.
  Реальный Linux/Jetson subprocess и длинный Goal smoke ещё не выполнены —
  пункт остаётся открытым.

- 2026-09-22 ~05:30 +05: №35 частично реализован: локальный Agent и фиксированный
  Jetson SSH helper получили `run_tests`/`run_lint` с обнаружением `pytest`/npm
  профилей, сроком до 300 с, ограниченным выводом, точным exit code и
  owner/session-scoped artifact при ошибке. Произвольная команда не принимается;
  execution permission и Plan-mode запреты сохранены. Живой локальный Agent
  на qwen3.6-35b вызвал `run_tests` на безопасном `/tmp` fixture. Проверка
  вскрыла два дефекта: ChromaDB-off/Terminus терял явно запрошенную схему, а
  после approval модель повторяла тот же вызов. Оба исправлены: схема
  загружается по явному запросу без постоянного расхода context budget,
  повтор точного одобренного действия не исполняется и не создаёт вторую
  карточку. Desktop/mobile Chromium после reload показывают одну `done`
  карточку и маркер результата без page errors. Полный pytest на этом срезе:
  6838 passed, 20 skipped, 105 subtests. Реальный Jetson и долгий Goal/Team
  smoke ещё не пройдены; чекбокс остаётся открытым.

- 2026-09-22 ~03:48 +05: в изолированном UI smoke устранены лишние
  `401 /api/auth/csrf` при явном `AUTH_ENABLED=false` и
  `404 /api/projects?limit=200` при отключённом Engineering. CSRF при
  включённой авторизации по-прежнему отклоняет анонимный запрос.
  Регрессионный тест прошёл; повторный Playwright-запуск показал
  `failed_responses: []`. Версии `projects.js` в app и SW согласованы.
  Полный pytest на этом срезе: 6800 passed, 20 skipped, 105 subtests.
  Это локальный патч, ещё не release/Jetson.
- 2026-09-22 ~03:51 +05: ревизия №34 подтвердила пробел: Agent по-прежнему
  использует `bash` для `git status`/`git diff`; Team `git.diff` относится
  только к управляемому worktree, общих typed `git_status`/`git_diff`/`git_log`
  нет. Во время изолированного shutdown зафиксированы предупреждения MCP
  `Attempted to exit cancel scope in a different task`: соединения создаются
  в startup task, а `AsyncExitStack.aclose()` вызывается из shutdown task.
  Требуется владелец lifecycle в той же задаче; простое подавление лога
  проблему не исправит. Оба пункта остаются открытыми.
- 2026-09-22 ~03:54 +05: stdio/SSE MCP теперь держат транспортную
  `AsyncExitStack` в задаче-владельце до сигнала disconnect. Регрессионный
  тест закрывает соединение из другой async-задачи и проверяет задачу-владельца;
  11 MCP-тестов прошли. Изолированный реальный web startup/shutdown закрыл
  `rag`, `image_gen`, `memory`, `email`, `builtin_browser` без предупреждений
  `Attempted to exit cancel scope in a different task`. HTTP/OAuth транспорт
  пока не переведён на этот lifecycle; общий пункт остаётся частичным.
  Полный pytest после изменения: 6801 passed, 20 skipped, 105 subtests.
- 2026-09-22 ~03:58 +05: HTTP/OAuth MCP переведён на задачу-владельца с
  ограниченным ожиданием handshake: `needs_auth` не уничтожает транспорт,
  позднее подключение публикует `connected`, а настоящий provider timeout
  остаётся ошибкой. Тесты моделируют позднюю авторизацию и закрытие из другой
  задачи; отдельные тесты проверяют быструю отмену pending OAuth и фактический
  HTTP transport context с одинаковой задачей входа/выхода. Локальный внешний
  OAuth endpoint для живого сетевого smoke пока отсутствует, поэтому
  end-to-end ветка остаётся непроверенной. Финальный полный локальный pytest:
  6805 passed, 20 skipped, 105 subtests. Повторный web startup/shutdown на
  текущем срезе штатно закрыл все пять встроенных MCP без cancel-scope warning.
- 2026-09-22 ~04:09 +05: начат №34: локальный Agent получил typed
  `git_status` с machine-readable staged/unstaged/untracked, HEAD hash,
  bounded выводом, фильтром чувствительных путей и запретом запуска на
  host-bound route без host adapter. Есть тесты на native conversion,
  unborn HEAD, rename, 100-file cap, секретный `.env`, out-of-scope path и
  лишние аргументы; узкие policy/index/regression тесты прошли (265 + 4).
  `git_diff`, `git_log`, host adapter и live Agent smoke ещё не готовы — №34
  остаётся открытым.
- 2026-09-22 ~04:13 +05: local Agent live smoke №34 на изолированном
  `:7130`, безопасный чат `cb75d28c-1158-420e-ab6b-cf1d86137e27` и
  тестовый repo `/tmp/odysseus-fixtures.gHICAb`: модель действительно вызвала
  `GIT_STATUS` при выключенном bash; durable `tool_events` содержит
  `tool=git_status`, `exit_code=0` и четыре ожидаемых untracked файла.
  Console errors отсутствовали. Добавлены тесты запрета host-bound fallback
  и управляющих символов в имени файла; пункт всё ещё частичный. Финальный
  полный pytest после правок: 6811 passed, 20 skipped, 105 subtests.
- 2026-09-22 ~04:13 +05: content-free production monitor зафиксировал переход
  двух running children в failed (итого 4 failed, 3 completed) при активной
  Goal и совпадающих live/durable cursors 21855/21855. Это технический
  инцидент для отдельной диагностики; содержимое задачи и child transcripts
  не читались, автоматического перезапуска не было.
- 2026-09-22 ~04:31 +05: реальный Safari перезагружен через его кнопку.
  Сначала UI кратко показывал `Новый чат готов` и 0%, затем `Loading chat`,
  после replay восстановились тот же session URL, 47% рабочего context,
  Goal active attempt 258, Plan executing 1/7, Subagents и Stop generation.
  Это подтверждает восстановление технического состояния; transient 0%
  остаётся UX-дефектом, а полноту thinking/replay это наблюдение не доказывает.
- 2026-09-22 ~04:32 +05: №34 расширен: локальные Agent `git_diff` и
  `git_log` дают bounded file patch/blob hashes и structured commit records;
  оба реально вызваны моделью в изолированном `:7130` чате
  `7c65715c-ec0d-43a3-b375-f45db564acca`, durable events с `exit_code=0`,
  bash выключен, console errors отсутствуют. Host adapter проведён через
  `host_execution`, `host_exec.py`, `host_runner.file.call` и model path guard;
  отрицательные тесты закрывают `.env`, out-of-scope, staged/unstaged и лимиты.
  206 focused passed; полный pytest текущего дерева: 6819 passed,
  20 skipped, 105 subtests. Реальный Jetson на релизном SHA ещё не проверен,
  поэтому №34 остаётся открытым.
- 2026-09-22 ~04:49 +05: устранён ложный стартовый `New chat ready`/0% при
  URL сохранённого чата: `startupShell` держит отдельный localized
  `Loading chat…` и скрывает непроверенный context pill до завершения
  `loadSessions`/`selectSession`; успех и отказ очищают busy-state.
  Desktop 1440×900 и mobile 390×844 Playwright с задержкой `/api/sessions`
  показали loading → один сохранённый message и авторитетные 9/128000 токенов;
  корневая страница по-прежнему открывается без failed responses. Отдельно
  SSE work-events теперь preflight-ит owner/session до HTTP 200: отсутствующий
  чат возвращает 404 вместо исключения внутри потока/500. Регрессионные тесты
  и полный pytest: 6822 passed, 20 skipped, 105 subtests. Изолированный
  тестовый чат ещё даёт ожидаемые 404 на отсутствие research/run; UI-пробы
  этих endpoints требуют отдельного улучшения.
- 2026-09-22 02:11 +05: начат шестичасовой content-free мониторинг Jetson и
  Safari; журнал `/tmp/odysseus-60item-soak-20260922.log`, минимальный рубеж
  08:11 +05. Это начало наблюдения, не финальная приёмка.
- №31 — **частично**: локальный и host-runner `read_file` возвращают SHA-256,
  размер, encoding/binary, line/byte range и optional line numbers; данные
  бинарного файла не попадают в `output`. Локальный инструмент теперь сохраняет
  обрезанный большой UTF-8 файл в owner/session-scoped observation artifact с
  проверкой хеша и постраничным `read_tool_artifact`; отрицательный тест
  проверяет изоляцию другого owner. Регрессионные проверки: 41 passed;
  полный pytest на предыдущем срезе: 6779 passed, 20 skipped, 105 subtests.
  Host-runner artifact path добавлен в следующем срезе; ещё нужен живой
  Agent/Goal smoke на релизном SHA перед закрытием пункта.
- 2026-09-22 ~02:20 +05: перезагрузка реального Safari восстановила тот же
  чат, Goal, Plan 3/9, Subagents, Stop и 52% контекста после короткого loading
  состояния 0%; переписка не менялась. Это smoke восстановления UI, не полная
  проверка replay/thinking.
- 2026-09-22 ~02:25 +05: исправлена потеря v2-диапазонов в native
  function-call адаптере. Host-runner теперь передаёт большие файлы через
  закрытый chunk transport, сервер сверяет хеш и создаёт owner/session-scoped
  artifact; модель видит только bounded preview. Добавлены отрицательные тесты
  на изменившийся файл и анонимный scope. Полный pytest: 6782 passed,
  20 skipped, 105 subtests; Jetson и live Agent/Goal на новом коде ещё не
  проверены, поэтому №31 остаётся открытым.
- 2026-09-22 ~02:34 +05: усилена проверка host-ветки: тест проходит через
  настоящий `host_exec.py` subprocess и отдельно проверяет передачу owner и
  session_id из dispatcher. Затронутые suites: 79 passed, 45 subtests;
  `git diff --check` чистый. Live smoke на Jetson не выполнялся.
- №32 — **частично**: добавлен `search_files` для локального и host-runner
  исполнения. По умолчанию возвращает постраничный список разных файлов,
  `mode=matches` — строки совпадений; применяет существующие ограничения
  workspace/sensitive paths, `rg` semantics локально и bounded Python fallback.
  Проверены native-вызов, pagination 62 файлов, fallback, scope и host seam.
  Полный pytest на этом срезе: 6791 passed, 20 skipped, 105 subtests; точный
  контрактный тест набора инструментов сабагента обновлён. Ещё нужен live
  Agent/Goal smoke на релизном SHA до закрытия.
- №33 — **частично**: `list_tree` даёт ограниченную по глубине/числу записей
  иерархию с размерами без содержимого файлов; учитывает `.gitignore`, скрытые,
  generated и чувствительные пути. `file_outline` использует Python AST,
  возвращает строки классов/функций/методов и явно сообщает `unavailable` для
  остальных языков. Подключены локальный и host-runner пути, а явный запрос
  подгружает специализированные схемы через tool retrieval. Не включены в
  постоянное ядро: две дополнительные схемы в общем file-домене превысили
  маленький бюджет модели в route-тесте и вызвали `uncompactable` до первого
  запроса. После выделения их в deferred discovery route-регрессия проходит.
  Полный pytest после исправления route schema budget: 6797 passed,
  20 skipped, 105 subtests. Ещё нужен live smoke на новом коде, поэтому
  чекбокс остаётся открытым.
- 2026-09-22 ~03:04 +05: реальный Safari после reload восстановил прежний
  чат, Goal, Plan 1/7, Subagents, Stop и 48% контекста; это read-only smoke
  старого production SHA. Второй запущенный браузер Opera GX не имел открытой
  Odysseus-вкладки, чужие cookie/сессии не переносились.
- 2026-09-22 ~03:38 +05: на отдельном локальном `127.0.0.1:7130` с отдельной
  SQLite и безопасными `/tmp`-фикстурами проведён реальный Chromium Agent smoke
  с qwen3.6-35b на 192.168.50.4. `list_tree`, `file_outline`, `search_files`
  (страницы 0/1), `read_file` большого файла и `read_tool_artifact` выполнены
  с `exit_code=0`; проверены persisted tool events и handle в видимом preview.
  Предыдущий live-прогон вскрыл потерю selected read tools при Terminus clamp,
  а также owner/session при MCP fallback: оба механизма исправлены и закреплены
  регрессионными тестами. Это локальный smoke, не Jetson release acceptance.
- 2026-09-22 ~03:40 +05: повторный безопасный Agent smoke на изолированном
  сервере подтвердил `exit_code=0` для `list_tree` (игнорируемый файл скрыт),
  `file_outline` (есть `Widget.run`), `search_files` (две разные страницы),
  `read_file` большого файла (2123 символа preview + artifact handle) и
  `read_tool_artifact` по тому же handle. Устранён обнаруженный в live-тесте
  сброс выбранных read-only схем при Terminus clamp и потеря owner/session в
  legacy MCP fallback. Полный pytest: 6799 passed, 20 skipped, 105 subtests.
  Production/Jetson остаётся на старом SHA, поэтому пункты 31–33 не закрыты.
- Локальный auth-disabled UI при загрузке всё ещё получает ожидаемый/необработанный
  `401 /api/auth/csrf` и `404 /api/projects?limit=200`; консоль не полностью
  чистая. Разобрать перед общим QA gate, не выдавать текущий smoke за полный.
- Остановка изолированного web-процесса выдаёт предупреждения MCP shutdown
  `Attempted to exit cancel scope in a different task than it was entered in`
  для встроенных серверов. Проверить lifecycle и отсутствие утечки процессов.

## P0 — наблюдаемость и восстановление

- 2026-09-22: Safari production chat `d0cb45f4-e586-4662-84b9-a8ebc37829e6`
  показывал семь одинаковых `Context checkpoint failed` в 16:49–16:59 и Goal
  оставалась `active` (attempt 308). Это подтверждает повторный запуск после
  failed checkpoint на установленной версии. Локальный route теперь ставит
  Goal в `waiting_user` после первой такой terminal failure, сохраняя ledger и
  `failure_code`; failing-first SSE-route и durable-store regression прошли
  (49 адресных тестов, 2 subtests; полный pytest 7001 passed, 20 skipped,
  109 subtests). Первопричина summarizer failure на Jetson
  не установлена: SSH отклонён, `192.168.50.4:1234` с Mac недоступен. Боевой
  run не возобновлялся; локальный патч не развернут.
- 2026-09-22: найден воспроизводимый механизм summarizer fallback. В
  `compact_working_context` есть общий deadline, но первый Utility вызов
  получал тот же полный timeout и мог исчерпать его до выбранной модели чата.
  Failing-first тест с зависшей Utility показал, что selected route не
  вызывается. Теперь deadline делится между маршрутами, с резервом для
  reasoning-only retry внутри каждого; отдельный failing-first тест защищает
  единственную медленную модель от слишком короткого первого тайм-аута.
  63 контекстных теста и 2 subtests прошли; полный pytest — 7003 passed,
  20 skipped, 109 subtests passed. Это подтверждённый локальный
  дефект, но не доказанная первопричина конкретного Jetson-инцидента.
- 2026-09-22: failure diagnostics для checkpoint теперь проходят весь путь
  summarizer → `context_compaction_failed` SSE → Goal checkpoint → owner-scoped
  wait panel. Ранее route обнулял код при присваивании terminal metadata;
  failing-first route test это воспроизвёл. Публичные значения только из
  фиксированного allowlist (`summarizer_timeout`, transport, 429, unavailable,
  no-answer, context budget и др.); provider body не выводится. Панель больше
  не говорит «несколько раз», когда Goal ждёт после первой ошибки. 71 адресный
  тест и 2 subtests прошли; полный pytest 7006 passed, 20 skipped,
  109 subtests passed. JS syntax и `git diff --check` чистые; PWA assets
  versioned. Live Jetson диагностика пока не подтверждена.
- 2026-09-22: уточнена классификация checkpoint failures. Внутренний
  `HTTP 502` с `no answer content` теперь получает `summarizer_no_answer`, а
  не общий provider error. Failing-first тест обнаружил, что timeout probe
  окна контекста вообще вырывался из Agent generator; теперь он даёт
  `context_compaction_failed` с безопасным `context_window_unavailable`,
  terminal failure и сохранение Goal. Лог soft-trim больше не печатает
  произвольный текст исключения. 92 адресных теста/2 subtests прошли;
  полный pytest 7007 passed, 20 skipped, 109 subtests passed;
  production Jetson всё ещё не проверен.
- 2026-09-22: повторный content-free Safari snapshot: тот же production чат
  показывал Goal `active`, attempt 326 вместо 308; видимый хвост по-прежнему
  заканчивался одинаковыми checkpoint failures в 16:49–16:59, context 42%.
  Действия в боевой Goal не отправлялись. Локальный wait panel больше не
  предлагает сразу `resume_goal` после checkpoint failure: действие
  `inspect_context` открывает context pill, код причины остаётся виден;
  если pill недоступна, выводится понятный fallback. 53 адресных теста прошли,
  полный pytest 7007 passed, 20 skipped, 109 subtests passed;
  JS syntax и `git diff --check` чистые. PWA URLs синхронизированы.
- 2026-09-22: terminal Goal callback имел второй, независимый HTTP dispatch
  рядом с `src.goal_controller.dispatch_goal_continuation`. Он обходил
  общую проверку unknown effects и при отказе снова рекурсивно ставил retry,
  а также логировал первые 1000 байт server response. Failing-first route
  test подтвердил, что callback не вызывал общий контроллер. Теперь terminal
  callback после backoff вызывает единый fenced dispatcher: exact lease,
  unknown-effect guard и `force_wait_user` на отказе dispatch. Статический
  тест обновлён по новой архитектуре. 50 профильных тестов прошли; полный
  pytest — 7008 passed, 20 skipped, 109 subtests passed. Live acceptance
  после этой правки ещё не выполнен.
- 2026-09-22: stale terminal callback race закрыта локально. Failing-first
  route test показал, что `done/error` от старого run запускал dispatch или
  записывал failure уже после revise Goal. Callback теперь захватывает
  original Goal ID + attempt; `acquire_goal_lease` сверяет их в SQL CAS,
  `record_goal_failure` не принимает старый attempt. Shared dispatcher
  получает эти значения и не делает модельный запрос при stale попытке.
  55 профильных тестов прошли; полный pytest 7016 passed, 20 skipped,
  109 subtests passed. Live multi-client race после этой правки ещё не
  выполнена.
- 2026-09-22: усилена маршрутная SSE фикстура: раньше она создавала только
  `sessions`, а `agent_runs._persist_run_state` писала `no such table:
  chat_run_states` в лог. Добавлены изолированные `ChatRunState` и
  `ChatMessage` таблицы и общий SessionLocal; route tests теперь напрямую
  проверяют durable terminal status, cursor и context snapshot. Все 8 тестов
  файла проходят без этих скрытых ошибок; полный pytest 7016 passed,
  20 skipped, 109 subtests passed. Это улучшает доказательность
  проверок, но не закрывает live/Jetson acceptance.
- 2026-09-22: `llm_core` превращает недоступный endpoint в фиксированную
  форму HTTP 503 `Cannot reach …`. Диагностика checkpoint ранее называла её
  общей ошибкой провайдера; failing-first тест показал неверный код. Теперь
  она получает `summarizer_transport_unavailable`, при обычном 503 остаётся
  `summarizer_provider_error`. SSE regression подтверждает, что адрес/текст
  сетевого исключения не попадает в публичный event. Live версия ещё старая.
- 2026-09-22: failing-first nonstream ConnectError test с фиктивными URL
  credentials и transport detail обнаружил утечку обоих в `llm_core` log и
  HTTP 503 detail. Connect path теперь логирует redacted endpoint и тип
  исключения, а ответ содержит только redacted host. 124 профильных теста и
  2 subtests прошли; полный pytest 7017 passed, 20 skipped, 109 subtests
  passed. Live build ещё не проверен.

- [ ] **01. Панель «Почему агент ждёт?»** Показывать текущий run/child, lease, модель и endpoint, фазу (`model`, `tool`, `approval`, `user`, `queue`, `reconnect`), длительность, последний durable checkpoint и безопасное действие восстановления. Проверить на зависшем запросе и двух клиентах.
- [ ] **02. Unified run inspector.** Дерево parent → children → tool calls → artifacts с точными ID, временами, статусами и cursor; переход из Goal/Plan/Subagent UI к конкретному событию.
  - 2026-09-23: локально добавлен owner-scoped bounded inspector с курсорами
    parent run, child event, replay event и ленивым раскрытием child evidence.
    В изолированном браузерном fixture с 205 событиями проверены два клиента,
    1280×800/390×844, переход из Why waiting/Goal/Plan/Subagents, старые
    страницы и reload. Боевой Jetson и реальный Agent-run на выбранной модели
    ещё не проверены; пункт остаётся открытым до релизного smoke.
- [ ] **03. Классификация ошибок и retry policy.** Разделить timeout, rate limit, provider unload, schema mismatch, transport, context, unknown side effect; повторять только доказанно безопасные запросы с jitter и budget.
- [ ] **04. Watchdog полезного прогресса.** Отдельно отслеживать heartbeat и реальные изменения: step, artifact, тест, diff, evidence. Оживший SSE не должен считаться прогрессом задачи.
- [ ] **05. Детектор зацикливания.** Ловить одинаковые action→observation, повторные ошибки, A↔B циклы, монолог, бессмысленное повторное сжатие; сначала диагностический nudge, затем bounded escalation, не бесконечный автоповтор.
- [ ] **06. Durable recovery matrix.** Автотесты на kill/restart в каждой точке: до tool start, после effect-intent, после tool result, во время compaction, approval, child join и terminal snapshot.
  - 2026-09-22: добавлены реальные subprocess `os._exit(17)` проверки на
    двух границах compaction (`compacted` summary и model-visible
    `context_checkpoint`) и на `ask_user` approval-wait. После restart
    проверены durable cursor, exact checkpoint, redaction replay, статус
    `interrupted`, отсутствие второго recovery и повторного исполнения.
    3 новых process-kill сценария прошли; полный pytest 7011 passed,
    20 skipped, 109 subtests passed. Child join и остальные границы
  матрицы ещё открыты; это не шестичасовой live soak.
  - 2026-09-22: повторная read-only Safari проверка production чата:
    видимый контекст 37 119 / 88 320 (42%), автосжатие 75% usable input
    (эффективно 49.1% окна), chat-scoped target после сжатия 15%,
    recent_groups 4, recent_tokens 2048, summary_tokens 1200, timeout 150 s.
    При неизменном видимом хвосте checkpoint failures Goal перешла с attempt
    337 на 338. Это подтверждает незакрытый live-дефект; низкий target — лишь
    гипотеза, не доказанная причина. Никаких настроек или run не меняли.
  - 2026-09-22: после восстановления интерактивного SSH доступа к Jetson
    read-only диагностика подтвердила установленный image
    `odysseus:release-64bd7e5` (healthy по Docker), но endpoint модели
    `192.168.50.4:1234` недоступен: host curl `/v1/models` timeout,
    контейнерный запрос — `No route to host`; ping по `eno1` и `wlP1p1s0`
    без ответа. В логах Odysseus примерно каждые 94 секунды идут
    `Context summarizer candidate failed (HTTPException)` →
    `Working context compaction failed` → `Configured context shaping failed`.
    Safari показывал очередные одинаковые остановки и attempt 337→338.
    Это подтверждает сетевую недоступность провайдера как текущий блокер
    summarizer; конкретный HTTP status старый image не логирует, поэтому
    не утверждаем, что единственная причина — именно транспорт. Сервис,
    маршруты, чат и Goal не менялись. Запрошена проверка LM Studio host.
- [ ] **07. Unknown-side-effect inbox.** Отдельная очередь неопределённых side effects с проверяемыми receipts и кнопками «проверено / не повторять / повторить после проверки».
- [ ] **08. Per-run resource budget.** Время, токены, запросы, CPU/RAM, tool calls и число children с видимым soft/hard limit; Goal при достижении лимита ждёт решения, а не молча останавливается.
  - 2026-09-22: optional `goal_max_model_requests` (0 = без лимита)
    считает фактические transport POST к модели до отправки, в том числе
    неудачный primary и попытку fallback. Достигнутый cap не начинает
    следующий POST, проходит через durable `budget_exceeded` с exact run ID,
    переводит Goal в `waiting_user` и виден в UI. Внутреннее событие
    отделено случайным per-run nonce; тест подтверждает, что провайдерский
    SSE не может выдать себя за budget event. Транспортные, Agent, route,
    store, UI и process-kill проверки: 168 профильных тестов, отдельно
    37 store tests и 77 соседних streaming/provider tests. Это не счётчик
    non-stream summarizer/reducer или
    сабагентов; время, CPU/RAM, общий child budget и soft limits открыты.
    Full pytest и Jetson live acceptance ещё не выполнены.
  - 2026-09-22: полный pytest выявил 35 order-dependent ошибок Goal-store
    после threaded route tests (`sqlite3: no such table: sessions`).
    `owned_chat` fixture теперь явно восстанавливает схему in-memory SQLite,
    не полагаясь на import-time `init_db`. Воспроизводивший порядок дал
    51 passed; повторный полный прогон завершился: 7043 passed,
    20 skipped, 9 warnings, 109 subtests passed. Это локальная
    проверка, не Jetson/шестичасовая приёмка.
  - 2026-09-22: в общем непроверенном batch добавлен optional
    `agent_max_children_per_run`: runtime считает всех children точного
    parent run, включая завершённых/удалённых, и проверяет cap атомарно с
    созданием child. При исчерпании Agent отдаёт `children` budget event,
    сохраняет tool-result ledger и Goal должна перейти в `waiting_user`.
    Подтверждение тестами/живым запуском отложено до общего прогона по
    просьбе пользователя; пункт не закрыт.
  - 2026-09-22: добавлен optional hard limit `goal_max_total_tokens`
    (0 = без лимита), считающий prompt + completion usage по завершённым
    модельным раундам. При достижении лимита новый model request не
    начинается; `model_tokens used/limit` с exact run ID проходит через
    durable SSE, Goal `waiting_user` и UI. Failing-first producer/store/UI
    тесты, HTTP route/API contract и process-kill boundary прошли:
    52 адресных теста (включая 5 process-kill вариантов), отдельно
    36 store tests. Это per-attempt cap;
    soft limit, CPU/RAM, время, точный HTTP request count и children ещё
    открыты. Live/Jetson verification не выполнена.
  - 2026-09-22: backend без usage (включая возможные локальные endpoints)
    покрыт отдельным тестом: токеновый cap использует оценку и не начинает
    следующий model request. SSE/Goal checkpoint/UI теперь несут
    `usage_source` (`real`, `estimated`, `mixed`) и явно помечают оценку,
    чтобы не выдавать её за серверный счётчик. Адресные producer/route/store/UI
    проверки прошли; broad pytest и live acceptance ещё не повторялись.
  - 2026-09-22: первая короткая фраза активной Goal могла попасть в
    `direct_low_signal` path, минуя обычный agent loop и его hard budgets.
    Failing-first тест воспроизвёл один прямой model request вместо двух
    раундов; теперь active Goal всегда использует основной цикл. Проверены
    обе ветки: обычный round cap и остановка на токеновом cap до следующего
    запроса; direct low-signal без Goal сохраняет прежнее поведение.
    10 профильных и 32 соседних route/UI теста прошли. Точный HTTP request
    count ещё открыт: fallback-кандидаты не равны фактическим transport POST.
  - 2026-09-22: разделены лимиты обычного Agent и долгой Goal. Первичный
    тест выявил жёсткую подмену на 200; затем контракт уточнён: настройка
    `agent_max_rounds` остаётся per-message, новая `goal_max_rounds` —
    per-attempt (по умолчанию 200), с отдельным полем в UI и серверной
    валидацией. HTTP regression проверяет пользовательский лимит 4,
    default 200 и неизменность обычного Agent; API settings test проверяет
    сохранение и отказ от некорректного значения. 40 адресных тестов и
    35 store tests прошли отдельно. Полный pytest до этого среза:
    7021 passed, 20 skipped.
    Live/Jetson acceptance и остальные resource budget остаются открыты.
  - 2026-09-22: модельный round cap теперь такой же hard decision point,
    как tool-call cap. Ранее `rounds_exhausted` был только UI-событием:
    terminal controller запускал новую попытку Goal, обнуляя счётчик.
    Failing-first producer/route/store/process-kill/UI тесты это зафиксировали.
    Теперь событие содержит exact run ID и `model_rounds used/limit`,
    сохраняется durable cursor, Goal переходит в `waiting_user`, панель
    показывает отдельный лимит раундов. 80 профильных тестов прошли.
    Это не точный HTTP request count; время, токены, CPU/RAM, children,
    soft limits и live smoke ещё открыты.
  - 2026-09-22: частично закрыт hard limit вызовов инструментов. При
    `budget_exceeded` сохраняются точный `run_id`, durable cursor и checkpoint;
    активная Goal атомарно переходит в `waiting_user` с причиной
    `resource_budget` и видимыми `used/limit`. Controller не начинает новую
    попытку, пока пользователь явно не возобновит цель. Добавлены failing-first
    unit/route/UI и kill-recovery regression tests; адресный прогон:
    63 passed; полный pytest — 6998 passed, 20 skipped, 109 subtests passed;
    JS syntax и `git diff --check` чистые. Время, токены, запросы,
    CPU/RAM, children, soft limits и live-проверка ещё не закрыты.
  - Следующий concurrency regression: запоздавшее `budget_exceeded` от старой
    попытки Goal теперь не может перевести новую, уже исправленную попытку в
    `waiting_user`. Settlement сверяет stable Goal ID и attempt; route передаёт
    snapshot этой попытки. Failing-first тест воспроизвёл прежнее отсутствие
    fencing, затем 33 теста store/stream прошли; полный pytest — 6999 passed,
    20 skipped, 109 subtests passed. Live race пока не проверен.
- [ ] **09. Run health SLO.** Метрики TTFT, prefill, tool latency, durable lag, SSE reconnect, UI long tasks, compaction time/failure, child queue wait; алерты на нарушение заданных порогов.
  - 2026-09-23: bounded tool-start telemetry: в длительном run незавершённые
    замеры latency ограничены 256 ключами и сроком 1 час; старые или потерянные
    результаты не создают ложную latency. Regression test покрывает 300 вызовов
    и поздний output. Сам пункт остаётся открытым до проверки всех SLO/алертов.
  - 2026-09-23: admin SLO теперь читает только allowlisted числовые поля
    `health_metrics` активных durable runs; мониторинг не теряет TTFT/tool/
    compaction/SSE показатели, если запрос попал в другой web worker.
    SQLite fixture содержит секреты в соседних JSON полях и подтверждает,
    что их нет в отчёте; PostgreSQL SQL projection проверена компиляцией.
    Live multiworker и пороги UI long-task/child queue ещё не приняты.
- [x] **10. Экспорт технического инцидента.** Один owner-scoped архив со схемой событий, версиями, обезличенными метриками и ошибками без содержимого чата/секретов; воспроизводимый test fixture.

## P0 — безопасные изменения и выпуск

- [ ] **11. Checkpoints изменений для Agent/Goal — расширить существующие Team file checkpoints.** Снимок agent-owned diff перед пачкой мутаций; выборочный откат только при совпадении after-hash; пользовательские правки не затирать.
- [ ] **12. Review-gate перед выпуском.** Независимый reviewer сверяет задачу, diff, тесты, риски и unknown outcomes; выдаёт адресные замечания или evidence-backed verdict до deploy/push.
- [ ] **13. Изолированные worktrees для пишущих сабагентов — расширить Team Git primitive.** Каждый child получает отдельный tree; parent видит conflict/diff/test status и принимает изменения явно; поддержать cross-host без общей writable директории.
- [ ] **14. Atomic edit + syntax gate.** `edit_file`/`apply_patch` проверяют old-hash и точное совпадение патча, затем parser/linter; при ошибке не оставляют частично применённый файл.
- [ ] **15. Shared canonical-path mutation queue.** Все write/edit/patch, fused и обычные, проходят одну очередь по canonical path; mutation→verification не может быть перебита другим writer.
- [ ] **16. Предпросмотр действия.** Для файла — diff, для shell — cwd/host/command/effect class, для сетевого POST — target/payload summary; разрешение привязывается к точному hash действия.
- [ ] **17. Release gate как состояние.** Backup → candidate → smoke → switch → post-switch checks → promote/rollback с durable receipts и exact SHA; никакого «успеха» до всех проверок.
- [ ] **18. Проверка рисков зависимостей и секретов в diff.** Перед commit/deploy запускать pin/SBOM/license/secrets checks; выдавать реальные находки с evidence, без автоматического удаления данных.

## P1 — контекст и эффективность моделей

- [ ] **19. Budgeted repo map.** Tree-sitter/LSP symbols и связи файлов, графовое ранжирование, 1–4K token budget по модели и запросу; инвалидация после изменения файла.
- [ ] **20. Symbol tools.** `find_symbol`, `find_references`, `go_to_definition`, `outline_file`, `diagnostics` через реально доступный LSP или локальный AST fallback; явное `unavailable` вместо выдуманной поддержки.
- [ ] **21. Deferred tool discovery.** Стабильное ядро маленьких tool schemas плюс поиск/загрузка нужной capability группы по запросу; измерять точность вызовов и сэкономленные schema tokens.
- [ ] **22. Версионированный tool registry.** Инвокация захватывает точную схему/политику; обновление registry применяется к следующему раунду без потери `bash`/`read_file` посреди Goal.
- [ ] **23. Контекстный budget waterfall.** UI отдельно показывает window, output/safety/schema reserve, trigger basis, protected messages, recent tail, summary, actual model-visible tokens и причину compaction.
- [ ] **24. Feasibility preview перед compaction.** Проверить protected groups, native tool pairs, минимальный целевой размер и доступность summarizer до расхода модели; если безопасного среза нет — не объявлять сжатие успешным.
- [ ] **25. Summary quality checks.** После сжатия машинно проверить сохранение Goal, критичных constraints, решений, незавершённых работ, file hashes и tool outcomes; сравнить hash/revision до и после.
- [ ] **26. Prompt-cache observability.** Измерять hit/miss и стабильность префикса по provider/model; не вставлять динамические поля в начало prompt без необходимости.
- [ ] **27. Dynamic model routing.** Раздельные профили planner/editor/reviewer/reducer; переключать только на границе round, пересчитывая окно и budget; показывать фактически выбранную модель и причину.
- [ ] **28. Model capability probes.** Проверять native tool calls, JSON schema, vision, context window, parallel requests, cancellation и streaming на каждом endpoint; результаты с TTL и ручным refresh.
- [ ] **29. Cache экономии в терминах time-to-success.** A/B измерять не только токены и tok/s, но число исправных tool calls, повторных действий, качество результата, длительность и вероятность завершения.
- [x] **30. Long-run replay corpus.** Сохранять обезличенные эталонные траектории 1K/10K/100K событий; регрессии контекста, reducer и UI проверять детерминированным replay.

## P1 — инструменты, которыми пользуется модель

- [ ] **31. `read_file` v2.** Диапазон строк/байт, line numbers, encoding/binary indication, файл-хеш и запрет выдачи мегабайт в prompt; большие данные — artifact handle.
- [ ] **32. `search_files` v2.** `rg`-семантика с кратким списком файлов по умолчанию, затем точные совпадения по запросу; лимиты результатов, owner/project scope и pagination.
- [ ] **33. `list_tree`/`file_outline`.** Быстрая иерархия с ignore rules, symbol outline и оценкой размера без чтения тела файла.
- [ ] **34. `git_status`/`git_diff`/`git_log` как typed tools.** Machine-readable staged/unstaged/untracked, hashes и file-level diff; безопаснее и дешевле постоянного вызова `bash`.
- [ ] **35. `run_tests`/`run_lint` как typed tools.** Обнаружение доступных профилей, bounded output, failed-test artifacts и точный exit code; не выдавать «passed» при timeout.
- [ ] **36. `inspect_process`/`inspect_port`/`tail_log`.** Read-only диагностика зарегистрированного хоста, bounded tail и PID/start-time fencing.
- [ ] **37. `http_probe` read-only.** DNS/TLS/status/headers/latency с зарегистрированными целями и SSRF-защитой; не превращать в произвольный POST-инструмент.
- [ ] **38. Browser toolset.** Навигация, DOM/accessibility snapshot, click/type, screenshot, network/console errors и запись короткого воспроизводимого сценария; отдельная browser permission scope.
- [ ] **39. `read_artifact`/`search_artifacts`.** Поиск по полным tool outputs без повторной отправки всего содержимого модели; ссылки остаются owner-scoped и привязаны к run.
- [ ] **40. `compare_files`/`verify_hashes`.** Сравнение exact before/after, нормализованные diff и проверка утверждений модели о файлах.
- [ ] **41. Package/toolchain diagnostics.** Read-only версии Python/Node/Git/compiler/LSP/container runtime и доступность сети; пригодно для выбора правильного test profile.
- [ ] **42. Structured tool errors.** Единые коды `not_found`, `permission_denied`, `transport_unavailable`, `stale_revision`, `unknown_outcome`, `timeout` плюс безопасный next action.
  - 2026-09-22: в общем batch исправлен путь ошибок без `exit_code`:
    `error`/`code` больше не пропускают нормализацию только из-за nullable
    exit code. Отрицательный тест добавлен; общий прогон ожидается вместе
    с остальными изменениями.

## P1 — агенты, команда и UX

- [ ] **43. Профили специализированных агентов.** `researcher` (read-only), `implementer`, `tester`, `reviewer`, `release` — модель, разрешённые tools, budget, result schema и triggers; project-scoped, без скрытого повышения полномочий.
- [ ] **44. Architect→editor workflow.** Архитектор предлагает изменение и критерии; исполнитель получает узкую спецификацию и правит; reviewer проверяет diff. Разрешить одну или разные модели.
- [ ] **45. Structured child contracts.** Objective, assigned context, expected artifact, acceptance checks, deadline и authority; parent получает краткий verdict + ссылки на evidence, не весь child transcript.
- [ ] **46. Join/queue UI.** Показывать running/queued/blocked children, модель/slot, time-to-first-token и причину ожидания; `wait_any` по умолчанию, `wait_all` только явный final join.
- [ ] **47. Fair scheduler по endpoint.** Per-model concurrency из настроек плюс глобальный RAM/VRAM cap, backpressure и aging; проверять фактические параллельные HTTP запросы к LM Studio.
- [ ] **48. Child cancellation fencing.** Stop child останавливает точный model/tool run; поздний result не может воскресить остановленного ребёнка или перезаписать parent state.
- [ ] **49. Роли и полномочия в UI.** В каждом child видны inherited access mode, запрещённые tools и изолированный context budget; повышать полномочия из child нельзя.
- [ ] **50. Cross-device task handoff.** Один canonical run/server state, newest-first initial view, cursor reconciliation и одинаковые token/thinking/tool карточки после reconnect.
- [ ] **51. Пользовательские steering notes без паузы Goal.** Guidance, ответ на `ask_user`, revise и явная pause — разные операции с разными CAS revision и тестами.
- [ ] **52. Inline code review.** Комментарии на конкретной строке diff и ответ агента «исправлено/не согласен + evidence» без копирования в чат.

## P2 — расширяемость и исследовательские функции

- [ ] **53. Trusted lifecycle hooks.** `BeforeTool`, `AfterTool`, `BeforeModel`, `AfterModel`, `PreCompress`, `AfterAgent`; JSON contracts, timeout, логи и явное включение. Project hooks из недоверенного repo не запускать автоматически.
- [ ] **54. Reproducible agent recipes.** Версионированный набор профиля, models, tools, skills, limits и acceptance gates для повторяемых задач.
- [ ] **55. ACP-compatible agent bridge.** Исследовать стандартный адаптер к внешним агентам как отдельным backend, сохраняя owner scope, permissions и event ledger; не смешивать их скрытые права с Odysseus.
- [ ] **56. Browser session recording.** Опциональная, ограниченная по времени запись DOM/action/screenshot для UI-багов с redaction; replay в isolated тесте.
- [ ] **57. Trajectory inspector.** Просмотр одного run как последовательности decisions/tool/result/diff/checkpoint с фильтрами, ссылками на artifact и сравнением двух траекторий.
- [ ] **58. Evaluation workbench.** Frozen задачи и acceptance criteria, A/B по моделям/профилям, held-out проверки, статистика неудач; никакого автодеплоя победителя.
- [ ] **59. Accessibility/reduced-motion профиль.** Плавающие панели и раскрытие блоков должны быть управляемы клавиатурой, не перекрывать друг друга и не грузить GPU в фоне.
- [ ] **60. Documented capability inventory.** Машинно формируемая страница «работает / experimental / unavailable на этом хосте» для LSP, DAP, browser, worktree, sandbox, MCP и моделей.
  - 2026-09-22: в общем batch добавлены консервативный owner-scoped JSON
    `/api/codex/inventory` и карточка в Settings → Tools. Источники —
    feature flags, обнаруженные LSP-бинарники и конфигурация MCP/моделей;
    конфигурация помечается `experimental`, не `working`, пока нет живого
    probe. DAP и cross-host worktree остаются `unavailable`; пути/ключи
    endpoint не выдаются; bearer-токену требуется `chat` scope. RU/EN подписи добавлены. Общий тестовый прогон,
    Browser/Safari smoke и документация ещё открыты.

## Исходники идей

- [Codex app](https://openai.com/index/introducing-the-codex-app/) — worktrees, review, skills, automations.
- [OpenHands Agent Canvas](https://github.com/OpenHands/docs/blob/main/docs/openhands/usage/agent-canvas/overview.mdx) и [SDK](https://docs.openhands.dev/sdk/guides/context-condenser) — backend-owned conversations, context condensation; [stuck detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector), [parallel tool execution](https://docs.openhands.dev/sdk/guides/parallel-tool-execution), [browser tools](https://docs.openhands.dev/sdk/guides/agent-browser-use).
- [Aider repo map](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md) и [architect/editor](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/usage/modes.md).
- [SWE-agent ACI](https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md) и [trajectory inspector](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/inspector.md).
- [Gemini CLI hooks](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/index.md) и [checkpointing](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/checkpointing.md).
- [OpenCode agents](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/agents.mdx) — специализированные роли и tool permissions.
- [goose recipes](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/recipes/recipe-reference.md) — воспроизводимые конфигурации агентов и extensions.

Правило реализации: каждый пункт получает контракт, отрицательные тесты, live
smoke на отдельном тестовом чате и проверку сохранения основного чата. Приоритет
не означает автоматическое разрешение рискованных действий или обход защиты.
