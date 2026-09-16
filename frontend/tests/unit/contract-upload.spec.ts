/**
 * 合同列表页的上传入口（P14-5-1）。
 *
 * 上传这一跳的**全部结果**就是"拿到 202 + task_id 并跳去工作台" —— 页面在这里
 * 不等待、不判断审查结果（那是工作台轮询的事）。因此这里钉住三件事：
 *
 * 1. 提交时把**文件与三个表单字段**交给 ``startContractReview``（字段名错一个就是 422）
 * 2. 受理成功后跳到 ``/review-tasks/{task_id}/workbench`` —— 用的是 Agent 返回的
 *    **真实** task_id，不是本地编的
 * 3. 失败时留在页面上如实报错，**不跳转**（跳过去只会看到一个不存在的任务）
 */
import { DOMWrapper, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import { ApiError } from '@/api/request'
import type { ContractListItem } from '@/api/contracts'
import { routes } from '@/router'
import ContractListView from '@/views/contracts/ContractListView.vue'

const getContractsMock = vi.fn()
vi.mock('@/api/contracts', () => ({
  getContracts: () => getContractsMock(),
}))

const startReviewMock = vi.fn()
vi.mock('@/api/agent-review', () => ({
  ACCEPTED_UPLOAD_EXTENSIONS: '.docx',
  startContractReview: (input: unknown) => startReviewMock(input),
}))

const ITEM: ContractListItem = {
  contract_id: 1,
  contract_no: 'HT-2026-001',
  title: '设备采购合同',
  contract_type: 'PURCHASE',
  created_at: '2026-09-15T10:00:00',
  latest_task: null,
}

/**
 * 挂载页面。
 *
 * ⚠️ 对话框里的元素要用 ``dialog()``（``document.body``）去查 —— ``el-dialog`` 把内容
 * **传送**到 body，``wrapper`` 里看不到。
 *
 * ⚠️ 也**不能**用 ``stubs: { teleport: true }`` 把它留在原地：那个替身会让
 * ``el-select`` 在对话框**再次打开**时陷入递归更新（"Maximum recursive updates
 * exceeded in <ElSelect>"）。这是桩件与 Element Plus 的相互作用，不是页面代码的问题 ——
 * 真实浏览器里没有这个替身，所以测试挪到真实渲染上，与线上行为一致。
 */
async function mountView(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push('/contracts')
  await router.isReady()

  const wrapper = mount(ContractListView, {
    global: { plugins: [router, ElementPlus] },
    attachTo: document.body,
  })
  await vi.waitFor(() => expect(wrapper.find('.el-table, .el-empty').exists()).toBe(true))
  return { wrapper, router }
}

/** 传送出去的那部分 DOM（对话框）。 */
function dialog(): DOMWrapper<Element> {
  return new DOMWrapper(document.body)
}

/** 在给定容器里按文字找按钮。 */
function buttonByText(root: DOMWrapper<Element> | VueWrapper, text: string) {
  return root.findAll('button').find((button) => button.text().includes(text))
}

/** 打开上传对话框。 */
async function openDialog(wrapper: VueWrapper): Promise<void> {
  await buttonByText(wrapper, '上传合同')!.trigger('click')
  await nextTick()
  await nextTick()
}

/** 模拟用户选中一个文件（Element Plus 的 input[type=file] 藏在 el-upload 里）。 */
async function selectFile(name = 'contract.docx'): Promise<File> {
  const file = new File([new Uint8Array([1, 2, 3])], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  })
  const input = dialog().find('input[type="file"]')
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
  await nextTick()
  return file
}

/** 选文件 + 填合同编号与名称（类型默认就是 PURCHASE，不用动）。 */
async function completeForm(): Promise<File> {
  const file = await selectFile()
  const inputs = dialog().findAll('.el-dialog input.el-input__inner')
  await inputs[0]!.setValue('HT-2026-001')
  await inputs[1]!.setValue('设备采购合同')
  return file
}

/** 提交。 */
async function submit(): Promise<void> {
  await buttonByText(dialog(), '上传并开始审查')!.trigger('click')
  await nextTick()
}

beforeEach(() => {
  getContractsMock.mockReset()
  getContractsMock.mockResolvedValue([ITEM])
  startReviewMock.mockReset()
})

afterEach(() => {
  // 对话框是传送到 body 的，不手动清掉会跨用例互相干扰
  document.body.innerHTML = ''
})

describe('合同列表页：上传并发起审查', () => {
  it('提交后把文件与三个表单字段交给 Agent', async () => {
    startReviewMock.mockResolvedValue({ task_id: 55 })
    const { wrapper } = await mountView()

    await openDialog(wrapper)
    const file = await completeForm()
    await submit()

    expect(startReviewMock).toHaveBeenCalledTimes(1)
    expect(startReviewMock.mock.calls[0]![0]).toEqual({
      file,
      contractNo: 'HT-2026-001',
      title: '设备采购合同',
      contractType: 'PURCHASE',
    })
  })

  it('受理（202）后跳到该任务的**工作台**，用 Agent 返回的 task_id', async () => {
    startReviewMock.mockResolvedValue({ task_id: 55 })
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    await openDialog(wrapper)
    await completeForm()
    await submit()
    await vi.waitFor(() => expect(push).toHaveBeenCalled())

    expect(push).toHaveBeenCalledWith('/review-tasks/55/workbench')
  })

  it('没选文件就提交：不发请求，只提示', async () => {
    const { wrapper } = await mountView()

    await openDialog(wrapper)
    const inputs = dialog().findAll('.el-dialog input.el-input__inner')
    await inputs[0]!.setValue('HT-2026-001')
    await inputs[1]!.setValue('设备采购合同')
    await submit()

    expect(startReviewMock).not.toHaveBeenCalled()
    expect(dialog().text()).toContain('请先选择合同文件')
  })

  it('Agent 拒绝（如类型不支持）：显示 Agent 给的原因，且**不跳转**', async () => {
    startReviewMock.mockRejectedValue(
      new ApiError({ code: 'PARSE_UNSUPPORTED_TYPE', message: '不支持的文件类型', status: 422 }),
    )
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    await openDialog(wrapper)
    await completeForm()
    await submit()
    await vi.waitFor(() => expect(dialog().text()).toContain('发起审查失败'))

    expect(dialog().text()).toContain('该文件类型暂不支持审查（当前只支持 DOCX）')
    expect(push).not.toHaveBeenCalled()
    // 对话框还开着，用户可以改完再提交
    expect(dialog().text()).toContain('上传并开始审查')
  })

  it('表单校验不过（422 + 无业务码）：说"内容不合法"，不说"网络错误"', async () => {
    startReviewMock.mockRejectedValue(new ApiError({ code: 'NETWORK_ERROR', message: '请求失败', status: 422 }))
    const { wrapper } = await mountView()

    await openDialog(wrapper)
    await completeForm()
    await submit()
    await vi.waitFor(() => expect(dialog().text()).toContain('发起审查失败'))

    expect(dialog().text()).toContain('提交的内容不合法')
  })

  it('Agent 连不上：提示去检查后端，而不是"未知错误"', async () => {
    startReviewMock.mockRejectedValue(new ApiError({ code: 'BACKEND_UNREACHABLE', message: 'Agent 无法访问' }))
    const { wrapper } = await mountView()

    await openDialog(wrapper)
    await completeForm()
    await submit()
    await vi.waitFor(() => expect(dialog().text()).toContain('发起审查失败'))

    expect(dialog().text()).toContain('Agent 连不上后端服务')
  })

  it('重新打开对话框时清掉上一次的输入', async () => {
    const { wrapper } = await mountView()

    await openDialog(wrapper)
    await completeForm()
    await buttonByText(dialog(), '取消')!.trigger('click')
    await nextTick()

    await openDialog(wrapper)

    const inputs = dialog().findAll('.el-dialog input.el-input__inner')
    expect((inputs[0]!.element as HTMLInputElement).value).toBe('')
    expect((inputs[1]!.element as HTMLInputElement).value).toBe('')
    expect(dialog().text()).not.toContain('contract.docx')
  })
})
