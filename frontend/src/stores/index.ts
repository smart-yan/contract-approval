/**
 * Pinia 实例。
 *
 * 单独导出实例（而不是在 main.ts 里内联 createPinia()）是为了让测试可以
 * 用同一个实例或各自创建新实例，见 tests/unit/stores.spec.ts。
 */
import { createPinia } from 'pinia'

export const pinia = createPinia()

export default pinia
