/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { AuthGuard } from '@/components/auth-guard'
import { BaseLayout } from '@/layout/base'
import NotFound from '@/pages/404'
import LoginPage from '@/pages/auth/login'
import Chat from '@/pages/chat'
import NewChat from '@/pages/chat/newchat'
import Index from '@/pages/index'
import KnowledgePage from '@/pages/knowledge'
import MemoryPage from '@/pages/memory'
// LitTrace 场景迁移：以下三个遗留功能页已停止注册路由（组件本身保留）。
// 恢复路由时请一并恢复 import：
// import DatabasePage from '@/pages/database'
// import NewsPage from '@/pages/news'
// import BiddingPage from '@/pages/bidding'
import {
  Navigate,
  Outlet,
  RouteObject,
  createBrowserRouter,
} from 'react-router-dom'

export type IRouteObject = {
  children?: IRouteObject[]
  name?: string
  auth?: boolean
  pure?: boolean
  meta?: any
} & Omit<RouteObject, 'children'>

export const routes: IRouteObject[] = [
  {
    path: '/',
    Component: Index,
  },
  {
    path: '/chat',
    children: [
      {
        path: '',
        Component: NewChat,
      },
      {
        path: ':id',
        Component: Chat,
      },
    ],
  },
  {
    path: '/knowledge',
    Component: KnowledgePage,
  },
  {
    path: '/memory',
    Component: MemoryPage,
  },
  // --- LitTrace 场景迁移：原「行业咨询助手」遗留功能页，暂不注册路由 ---
  // 页面组件（@/pages/database、@/pages/news、@/pages/bidding）与对应 API 模块
  // 均【未删除】，newchat.tsx 仍引用 @/api/news 的类型。恢复时取消注释即可。
  // 后端对应开关默认关闭：ENABLE_LEGACY_FEATURES / ENABLE_TEXT2SQL / ENABLE_NEWS_SCHEDULER。
  // {
  //   path: '/database',
  //   Component: DatabasePage,
  // },
  // {
  //   path: '/news',
  //   Component: NewsPage,
  // },
  // {
  //   path: '/bidding',
  //   Component: BiddingPage,
  // },
  {
    path: '/404',
    Component: NotFound,
    pure: true,
  },
]

export const router = createBrowserRouter(
  [
    {
      path: '/login',
      element: <LoginPage />,
    },
    {
      path: '/',
      element: (
        <AuthGuard>
          <BaseLayout>
            <Outlet />
          </BaseLayout>
        </AuthGuard>
      ),
      children: routes,
    },
    {
      path: '*',
      element: <Navigate to="/404" />,
    },
  ] as RouteObject[],
  {
    basename: import.meta.env.BASE_URL,
  },
)
