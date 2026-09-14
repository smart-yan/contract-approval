import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'
import { pinia } from './stores'

import 'element-plus/dist/index.css'
import './styles/index.css'

const app = createApp(App)

// 注意挂载顺序：pinia 必须在 router 之前，路由守卫/组件里才可能用到 store
app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')
