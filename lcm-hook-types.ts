// =============================================================================
//  LCM Hook System — Полные TypeScript-контракты
//  =============================================================================
//  Файл описывает все типы/интерфейсы данных, передаваемых через каждый хук,
//  а также полный пайплайн обработки события суммаризации (PreCompact).
//
//  Кодовая база: implements/lcm/src/
//  Точка входа хуков: src/hooks/dispatch.ts → dispatchHook()
//  Демон: src/daemon/server.ts (HTTP-сервер на порту 3737)
// =============================================================================

// ═══════════════════════════════════════════════════════════════════════════════
//  I. КОРНЕВЫЕ ТИПЫ — передаются через stdin каждого хука
// ═══════════════════════════════════════════════════════════════════════════════

/** Базовый контракт для ВСЕХ хуков. Claude Code пайпит JSON через stdin. */
interface HookBaseInput {
  /** ID сессии Claude Code (UUID) */
  session_id: string;
  /** Рабочая директория проекта */
  cwd: string;
}

// ─── 1. SessionStart → команда `lcm restore` ────────────────────────────

/** Входные данные хука SessionStart (lcm restore) */
interface SessionStartInput extends HookBaseInput {
  hook_event_name: "SessionStart";
}

/** Ответ демона на /restore */
interface RestoreResponse {
  /** Восстановленный контекст из предыдущих сессий */
  context: string;
  /** Пассивно захваченные инсайты из EventsDb */
  insights?: Array<{
    content: string;
    confidence: number;   // 0..1
    tags: string[];
  }>;
}

// ─── 2. UserPromptSubmit → команда `lcm user-prompt` ────────────────────

/** Входные данные хука UserPromptSubmit */
interface UserPromptSubmitInput extends HookBaseInput {
  /** Текст, который пользователь отправил Claude Code */
  prompt: string;
}

/** Запрос к демону на /prompt-search */
interface PromptSearchRequest {
  query: string;
  cwd: string;
  session_id: string;
  /** Размер инструкции обучения в байтах (для расчёта токен-бюджета) */
  learningInstructionBytes: number;
}

/** Ответ демона на /prompt-search */
interface PromptSearchResponse {
  /** Найденные подсказки из promoted-памяти */
  hints: string[];
  /** ID записей памяти, соответствующих hints */
  ids?: string[];
}

// ─── 3. PostToolUse → команда `lcm post-tool` ───────────────────────────

/** Входные данные хука PostToolUse */
interface PostToolUseInput extends HookBaseInput {
  /** Имя инструмента Claude Code: "Read", "Write", "Bash", "Edit", "Task"... */
  tool_name: string;
  /** Входные параметры инструмента (зависит от tool_name) */
  tool_input: Record<string, unknown>;
  /** Ответ инструмента (текст) */
  tool_response?: string;
  /** Выходные данные инструмента (если есть) */
  tool_output?: {
    isError?: boolean;
    [key: string]: unknown;
  };
}

/** Событие, извлечённое из вызова инструмента (extractPostToolEvents) */
interface PostToolEvent {
  type: "intent" | "file" | "git" | "error" | "decision" | "pattern" | "gotcha";
  category: string;
  priority: 1 | 2 | 3;  // 1=самый высокий (ошибки), 2=средний (git), 3=низкий
  data: string;
}

// ─── 4. PreCompact → команда `lcm compact --hook` ────────────────────────
//       ⚠️  ЭТО ГЛАВНЫЙ ХУК СУММАРИЗАЦИИ — см. раздел IV ниже

/** Входные данные хука PreCompact */
interface PreCompactInput extends HookBaseInput {
  hook_event_name: "PreCompact";
}

/** Запрос к демону на /compact */
interface CompactRequest {
  session_id: string;
  cwd: string;
  client: "claude";
  /** Пропустить ingest (true при session-end, т.к. ingest уже сделан) */
  skip_ingest?: boolean;
  /** Путь к transcript-файлу (.jsonl) */
  transcript_path?: string;
  /** Предыдущее содержимое суммаризации (для инкрементальной) */
  previous_summary?: string;
}

/** Ответ демона на /compact (возвращается в stdout хука) */
interface CompactResponse {
  /** Форматированное сообщение о результате компакшена */
  summary: string;
  /** Последнее созданное содержимое суммаризации (сырой текст) */
  latestSummaryContent?: string;
  /** Токенов до компакшена */
  tokensBefore?: number;
  /** Токенов после компакшена */
  tokensAfter?: number;
  /** Использованный LLM-провайдер */
  providerId?: string;
  providerLabel?: string;
}

// ─── 5. Stop → команда `lcm session-snapshot` ───────────────────────────

/** Входные данные хука Stop (периодический снепшот) */
interface SessionSnapshotInput extends HookBaseInput {
  /** Путь к transcript-файлу сессии */
  transcript_path: string;
}

// ─── 6. SessionEnd → команда `lcm session-end` ──────────────────────────

/** Входные данные хука SessionEnd */
interface SessionEndInput extends HookBaseInput {
  hook_event_name: "SessionEnd";
}

/** Ответ демона на /ingest */
interface IngestResponse {
  /** Количество новых сообщений, добавленных в БД */
  ingested: number;
  /** Общее количество токенов после инжеста */
  totalTokens?: number;
  /** Количество отредактированных (scrubbed) токенов */
  redacted?: number;
  /** Категории отредактированных данных */
  redactedCategories?: string[];
}

// ═══════════════════════════════════════════════════════════════════════════════
//  II. ТИПЫ БАЗЫ ДАННЫХ (SQLite)
// ═══════════════════════════════════════════════════════════════════════════════

type ConversationId = number;
type MessageId = number;
type MessageRole = "system" | "user" | "assistant" | "tool";
type SummaryKind = "leaf" | "condensed";
type ContextItemType = "message" | "summary";

/** Сообщение в разговоре (conversations + messages таблицы) */
interface MessageRecord {
  messageId: MessageId;
  conversationId: ConversationId;
  seq: number;            // порядковый номер в разговоре
  role: MessageRole;
  content: string;
  tokenCount: number;
  createdAt: Date;
}

/** Разговор (одна сессия) */
interface ConversationRecord {
  conversationId: ConversationId;
  sessionId: string;
  title: string | null;
  bootstrappedAt: Date | null;
  createdAt: Date;
  updatedAt: Date;
}

/** Суммаризация (summary_store, таблица summaries) */
interface SummaryRecord {
  summaryId: string;       // sum_<sha256-hex16>
  conversationId: number;
  kind: SummaryKind;       // leaf = из сырых сообщений, condensed = из других суммаризаций
  depth: number;           // глубина в DAG: 0 = листья, 1+ = конденсированные
  content: string;
  tokenCount: number;
  fileIds: string[];
  earliestAt: Date | null;  // самое раннее сообщение в этой ноде
  latestAt: Date | null;    // самое позднее сообщение в этой ноде
  descendantCount: number;
  descendantTokenCount: number;
  sourceMessageTokenCount: number;
  createdAt: Date;
}

/** Элемент контекстного окна (context_items) */
interface ContextItemRecord {
  conversationId: number;
  ordinal: number;         // порядок в контекстном окне
  itemType: ContextItemType;
  messageId: number | null;
  summaryId: string | null;
  createdAt: Date;
}

// ═══════════════════════════════════════════════════════════════════════════════
//  III. КОНФИГУРАЦИЯ КОМПАКШЕНА
// ═══════════════════════════════════════════════════════════════════════════════

interface CompactionConfig {
  /** Порог заполнения контекста как доля от бюджета (по умолчанию 0.75 = 75%) */
  contextThreshold: number;

  /** Количество "свежих" хвостовых сообщений, которые НЕ трогаем (по умолчанию 8) */
  freshTailCount: number;

  /** Минимальное число depth-0 суммаризаций для запуска condensation (3) */
  leafMinFanout: number;

  /** Минимальное число depth>=1 суммаризаций для condensation (2) */
  condensedMinFanout: number;

  /** Расслабленный минимум для hard-trigger sweeps (1) */
  condensedMinFanoutHard: number;

  /** Глубина инкрементальной компакшена после leaf-компакшена (0 = не делать) */
  incrementalMaxDepth: number;

  /** Целевое количество токенов для leaf-суммаризации (по умолчанию 600) */
  leafTargetTokens: number;

  /** Целевое количество токенов для condensed-суммаризации (по умолчанию 900) */
  condensedTargetTokens: number;

  /** Максимальное число раундов компакшена (10) */
  maxRounds: number;
}

type CompactionLevel = "normal" | "aggressive" | "fallback";

/** Результат одного вызова CompactionEngine.compact() */
interface CompactionResult {
  /** Была ли фактически выполнена компакшена */
  actionTaken: boolean;

  /** Токенов до компакшена */
  tokensBefore: number;

  /** Токенов после компакшена */
  tokensAfter: number;

  /** ID созданной суммаризации (если была) */
  createdSummaryId?: string;

  /** Была ли выполнена condensation */
  condensed: boolean;

  /** Уровень эскалации */
  level?: CompactionLevel;
}

/** Сигнатура функции суммаризации (LLM-вызов) */
type CompactionSummarizeFn = (
  text: string,
  aggressive?: boolean,
  options?: {
    previousSummary?: string;
    isCondensed?: boolean;
    depth?: number;
  },
) => Promise<string>;

// ═══════════════════════════════════════════════════════════════════════════════
//  IV. ПОЛНЫЙ ПАЙПЛАЙН СУММАРИЗАЦИИ (PreCompact)
//  ==============================================
//
//  Схема потока данных:
//
//  1. Claude Code детектит 85%+ заполнение контекстного окна
//  2. Claude Code вызывает PreCompact хук:
//       echo '{"session_id":"...","cwd":"...","hook_event_name":"PreCompact"}' | lcm compact --hook
//  3. dispatchHook("compact", stdin) в src/hooks/dispatch.ts:
//     - НЕ делает bootstrap (демон уже запущен SessionStart)
//     - validateAndFixHooks() — авто-исправление plugin.json если битый
//     - Создаёт DaemonClient на http://127.0.0.1:3737
//     - Вызывает handlePreCompact()
//  4. handlePreCompact() в src/hooks/compact.ts:
//     a. ensureDaemon() — проверяет что демон жив
//     b. Парсит stdin → {session_id, cwd, hook_event_name}
//     c. client.post("/compact", {session_id, cwd, client:"claude"})
//     d. firePromoteEventsRequest() — fire-and-forget промоушен событий
//     e. Возвращает CompactResponse.summary в stdout Claude Code
//  5. Демон обрабатывает POST /compact:
//     a. Валидация cwd, загрузка конфига
//     b. enqueue() — защита от гонок (одновременный компакшен сессии)
//     c. Создание ScrubEngine (фильтрация секретов)
//     d. Открытие БД → ConversationStore + SummaryStore
//     e. Если transcript_path и не skip_ingest:
//        - parseTranscript() → массив сообщений
//        - Определение дельты (новые сообщения с момента последнего storedCount)
//        - scrubWithCounts() → фильтрация секретов
//        - createMessagesBulk() → запись в БД
//        - appendContextMessages() → добавление в контекстное окно
//     f. Проверка tokenCount > 0
//     g. Создание CompactionEngine с конфигом:
//        new CompactionEngine(conversationStore, summaryStore, {
//          contextThreshold: 0.75,
//          freshTailCount: 8,
//          leafMinFanout: 3,
//          condensedMinFanout: 2,
//          condensedMinFanoutHard: 1,
//          incrementalMaxDepth: 0,
//          leafTargetTokens: config.compaction.leafTokens,
//          condensedTargetTokens: 900,
//          maxRounds: 10,
//          scrubber,
//        })
//     h. engine.compact({conversationId, tokenBudget:200_000, summarize, force:true})
//        → ВНУТРИ CompactionEngine.compact():
//
//           Шаг 1: Получить все context_items для conversationId
//           Шаг 2: Разделить на свежий хвост (freshTailCount=8) и остальное
//           Шаг 3: Посчитать токены ВНЕ хвоста
//           Шаг 4: Если токенов < threshold → actionTaken=false, выход
//           Шаг 5: Группировка сообщений в чанки по ~leafTargetTokens
//           Шаг 6: Для каждого чанка:
//                  a. Склеить текст сообщений
//                  b. Вызвать summarize(chunkText) → LLM генерирует summary
//                  c. Создать SummaryRecord в БД (kind="leaf", depth=0)
//                  d. Заменить сообщения в context_items на одну summary-ссылку
//           Шаг 7: Если leaf-суммаризаций >= leafMinFanout (3):
//                  a. Группировка leaf-суммаризаций в чанки
//                  b. summarize(chunk, false, {isCondensed:true, depth:1})
//                  c. Создать SummaryRecord (kind="condensed", depth=1)
//           Шаг 8: Повторять condensed-компакшен для depth >= condensedMinFanout (2)
//           Шаг 9: Если токенов всё ещё много → aggressive mode
//                  (уменьшенные целевые токены, принудительная condensation)
//           Шаг 10: Если агрессивный режим не помог → fallback:
//                   Простое усечение самых старых сообщений
//           Шаг 11: Обновить context_items (хвост + оставшиеся сообщения + все summary)
//           Шаг 12: Вернуть CompactionResult
//
//     i. justCompactedMap.set(session_id) — флаг на 30 секунд
//     j. buildCompactionMessage() → красивое ASCII-сообщение
//     k. Вернуть CompactResponse клиенту
//  6. handlePreCompact получает CompactResponse
//  7. Возвращает summary + latestSummaryContent в stdout
//  8. Claude Code вставляет этот stdout в системный промпт
//     → следующее сообщение модели видит результат суммаризации
// =============================================================================

// ═══════════════════════════════════════════════════════════════════════════════
//  V. ПОЛНЫЙ ЦИКЛ 6-ТИ ХУКОВ ВО ВРЕМЕНИ
// ═══════════════════════════════════════════════════════════════════════════════

/**
 *   Время ──────────────────────────────────────────────────────────────────▶
 *
 *   ┌──────────────┐
 *   │ SessionStart │  0 сек  — запуск сессии
 *   │ lcm restore  │           восстановление контекста + пассивные инсайты
 *   └──────┬───────┘
 *          │
 *   ┌──────▼───────┐
 *   │UserPrompt-   │  ~1 сек  — каждое сообщение пользователя
 *   │  Submit      │           поиск promoted-памяти + извлечение intents
 *   │lcm user-prompt│
 *   └──────┬───────┘
 *          │
 *   ┌──────▼──────┐
 *   │ PostToolUse │  ~0.5 сек — КАЖДЫЙ вызов инструмента (!)
 *   │ lcm post-tool│            извлечение пассивных событий в EventsDb
 *   └──────┬──────┘              (не блокирует Claude Code)
 *          │
 *          │   ... повторяется N раз ...
 *          │
 *   ┌──────▼──────┐
 *   │    Stop     │  ~60 сек  — периодический снепшот
 *   │lcm session- │            дельта-инжест транскрипта в демон
 *   │  snapshot   │            (throttled: не чаще интервала)
 *   └──────┬──────┘
 *          │
 *          │   ... ещё повторы ...
 *          │
 *   ┌──────▼──────┐
 *   │  PreCompact │  ~85%+    — контекст почти заполнен
 *   │lcm compact  │  токенов    ПОЛНЫЙ DAG-КОМПАКШЕН (см. раздел IV)
 *   │   --hook    │            LLM генерирует иерархические суммаризации
 *   └──────┬──────┘            контекст сжимается с 200K → 20-50K токенов
 *          │
 *          │   ... продолжение работы ...
 *          │
 *   ┌──────▼──────┐
 *   │ SessionEnd  │  конец    — финальный инжест + fire-and-forget:
 *   │lcm session- │  сессии     /compact (если не отключён)
 *   │    end      │            /promote (промоушен в долгую память)
 *   └─────────────┘            /promote-events (из EventsDb)
 *                              /session-complete (запись в манифест)
 */

// ═══════════════════════════════════════════════════════════════════════════════
//  VI. ИТОГОВАЯ ТАБЛИЦА КОНТРАКТОВ
// ═══════════════════════════════════════════════════════════════════════════════

/**
 *  ┌─────────────────┬────────────────────┬──────────────────────┬──────────────────────────────────────┐
 *  │ Хук Claude Code │ Команда LCM        │ Эндпоинт демона      │ Входной тип (stdin) → Ответ (stdout) │
 *  ├─────────────────┼────────────────────┼──────────────────────┼──────────────────────────────────────┤
 *  │ SessionStart    │ lcm restore        │ POST /restore        │ SessionStartInput → RestoreResponse  │
 *  │ UserPromptSubmit│ lcm user-prompt    │ POST /prompt-search  │ UserPromptSubmitInput → hints+инстр. │
 *  │ PostToolUse     │ lcm post-tool      │ (локально, не демон) │ PostToolUseInput → "" (события в БД) │
 *  │ PreCompact      │ lcm compact --hook │ POST /compact        │ PreCompactInput → CompactResponse    │
 *  │ Stop            │ lcm session-snap.. │ POST /ingest         │ SessionSnapshotInput → ""            │
 *  │ SessionEnd      │ lcm session-end    │ POST /ingest         │ SessionEndInput → ""                 │
 *  │                 │                    │ + /compact (fire&forget)                                     │
 *  │                 │                    │ + /promote                                                  │
 *  │                 │                    │ + /promote-events                                           │
 *  │                 │                    │ + /session-complete                                         │
 *  └─────────────────┴────────────────────┴──────────────────────┴──────────────────────────────────────┘
 */

export type {
  // Хуки
  HookBaseInput,
  SessionStartInput,
  UserPromptSubmitInput,
  PostToolUseInput,
  PreCompactInput,
  SessionSnapshotInput,
  SessionEndInput,
  // Ответы демона
  RestoreResponse,
  PromptSearchResponse,
  CompactResponse,
  IngestResponse,
  // Запросы к демону
  CompactRequest,
  PromptSearchRequest,
  // Пассивные события
  PostToolEvent,
  // БД
  ConversationId,
  MessageId,
  MessageRole,
  SummaryKind,
  ContextItemType,
  MessageRecord,
  ConversationRecord,
  SummaryRecord,
  ContextItemRecord,
  // Компакшен
  CompactionConfig,
  CompactionLevel,
  CompactionResult,
  CompactionSummarizeFn,
};
// =============================================================================
