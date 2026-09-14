/// <reference types="vite/client" />

/**
 * 环境变量类型声明。
 *
 * 只有在这里显式声明的变量才能在代码中以 `import.meta.env.XXX` 形式访问，
 * 否则 `npm run type-check` 会报错 —— 这能防止拼错变量名后静默拿到 undefined。
 */
interface ImportMetaEnv {
  /** 业务接口基础路径。见 .env.development（默认 /api/v1）。 */
  readonly VITE_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
