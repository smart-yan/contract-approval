/**
 * 时间格式化（P11-5）。
 *
 * 存在的理由只有一个：**后端返回的时间是 naive UTC**。
 *
 * 后端统一把 UTC 存成 ``DATETIME(3)``（见 ``backend/app/db/base.py`` 的约定 3），
 * 因此 JSON 里是这样一串：
 *
 * ::

 *     2026-09-15T10:00:00     ← 没有 Z，也没有 +08:00
 *
 * 而 ECMAScript 规定：**不带时区标识的日期时间串按本地时间解析**。
 * 于是 ``new Date('2026-09-15T10:00:00')`` 在东八区会被当成北京时间 10:00 ——
 * 比真实时刻（北京时间 18:00）**整整早了 8 小时**，而且不会报任何错。
 *
 * 所以这里显式补上 ``Z`` 再交给 ``Date``。这不是"猜"，而是把后端已经确定的
 * 语义（UTC）如实告诉浏览器 —— 补 ``Z`` 的那一行就是本模块存在的全部价值。
 *
 * ⚠️ 刻意**不引入日期库**：只做"补 Z + 按本地时区格式化"，几行就够，
 * 引一个 dayjs/moment 进来反而多一份依赖与一套口径。
 */

/** 已带时区标识的串（结尾的 ``Z`` 或 ``+08:00`` / ``-0500`` 之类）—— 有的话就不再补。 */
const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i

/**
 * 把后端的 naive-UTC 时间串解析成 ``Date``。
 *
 * 已经是带时区的串就原样解析（幂等：调两次结果一样）；无法解析时返回 ``null``，
 * 由调用方决定怎么显示 —— **不抛异常**，一个坏时间戳不该让整页渲染失败。
 */
export function parseUtcTimestamp(value: string | null | undefined): Date | null {
  if (!value) {
    return null
  }
  const normalized = HAS_TIMEZONE.test(value) ? value : `${value}Z`
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 格式化成本地时区的 ``YYYY-MM-DD HH:mm``。
 *
 * 解析不出来时**原样返回输入串** —— 显示一个看不懂的原始值，
 * 也比显示"Invalid Date"或干脆空白要好排查。
 */
export function formatUtcTimestamp(value: string | null | undefined): string {
  const parsed = parseUtcTimestamp(value)
  if (parsed === null) {
    return value ?? ''
  }
  return (
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
  )
}
