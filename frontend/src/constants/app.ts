/**
 * 应用级常量（壳工程）。
 *
 * 只放「前端外壳」需要的常量。业务常量（风险等级、任务状态等）属于后续业务阶段，
 * 且应当与后端 app/core/constants.py 的枚举字面值保持一致 —— 那部分在 P4 起再建立。
 */

export const APP_NAME = '合同审批审查系统'

export const APP_VERSION = '0.1.0'

/** 侧边栏展开宽度（px） */
export const SIDEBAR_WIDTH = 220

/** 侧边栏折叠宽度（px），el-menu collapse 模式下需要显式指定 */
export const SIDEBAR_COLLAPSED_WIDTH = 64
