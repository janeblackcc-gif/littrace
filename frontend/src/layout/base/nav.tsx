/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import IconHistory from '@/assets/layout/history.svg'
import IconHome from '@/assets/layout/home.svg'
import IconKnowledge from '@/assets/layout/knowledge.svg'
import IconMemory from '@/assets/layout/memory.svg'
import IconNewChat from '@/assets/layout/newchat.svg'
// LitTrace 场景迁移：原「行业资讯 / 招投标 / 数据库」入口已隐藏，对应图标不再使用。
// 恢复入口时请一并恢复以下 import：
// import IconBid from '@/assets/layout/bid.svg'
// import IconDatabase from '@/assets/layout/database.svg'
// import IconNews from '@/assets/layout/news.svg'
import React, { useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { message } from 'antd'
import { NavItem } from './nav-item'
import { SessionDrawer } from '@/components/session-drawer'
// LitTrace 场景迁移：行业选择器已隐藏，以下依赖暂不再使用。恢复时一并取消注释：
// import { Dropdown, message } from 'antd'
// import { DownOutlined } from '@ant-design/icons'
// import { industryState, setCurrentIndustry } from '@/store/industry'
import './nav.scss'

export function Nav() {
  const { pathname } = useLocation()
  const [sessionDrawerOpen, setSessionDrawerOpen] = useState(false)
  // 行业选择器隐藏后不再需要行业状态。恢复时取消注释：
  // const { currentIndustryId, industries } = useSnapshot(industryState)
  // const currentIndustry = useMemo(() => {
  //   const industry = industries.find((i) => i.id === currentIndustryId)
  //   console.log('[Nav] 当前行业:', industry?.name)
  //   return industry || industries[0]
  // }, [currentIndustryId, industries])
  // const industryMenuItems = useMemo(() => {
  //   return industries.map((industry) => ({
  //     key: industry.id,
  //     label: (
  //       <div className="industry-menu-item">
  //         <div className="industry-menu-item__name">{industry.name}</div>
  //         <div className="industry-menu-item__desc">{industry.description}</div>
  //       </div>
  //     ),
  //     onClick: () => {
  //       console.log('[Nav] 切换行业:', industry.id, industry.name)
  //       setCurrentIndustry(industry.id)
  //     },
  //   }))
  // }, [industries])

  const items = useMemo(
    () => [
      {
        key: 'home',
        label: '首页',
        icon: IconHome,
        href: '/',
      },
      {
        key: 'newchat',
        label: '新的聊天',
        icon: IconNewChat,
        href: '/chat',
      },
      {
        key: 'history',
        label: '对话历史',
        icon: IconHistory,
        href: '#',
        onClick: () => setSessionDrawerOpen(true),
      },
      {
        key: 'memory',
        label: '记忆库',
        icon: IconMemory,
        href: '#',
        onClick: () => message.info('暂未开放'),
      },
      {
        key: 'knowledge',
        label: '知识库',
        icon: IconKnowledge,
        href: '/knowledge',
      },
      // 暂时隐藏职业规划
      // {
      //   key: 'career',
      //   label: '职业规划',
      //   icon: IconCareer,
      //   href: '#',
      // },
      // --- LitTrace 场景迁移：以下为原「行业咨询助手」遗留入口，保留代码与路由组件、
      // --- 仅隐藏导航入口（对应后端 ENABLE_TEXT2SQL / ENABLE_LEGACY_FEATURES 默认 false）。
      // --- 如需恢复，取消注释即可；页面组件与 API 模块均未删除。
      // {
      //   key: 'database',
      //   label: '数据库',
      //   icon: IconDatabase,
      //   href: '/database',
      // },
      // {
      //   key: 'news',
      //   label: '行业资讯',
      //   icon: IconNews,
      //   href: '/news',
      // },
      // {
      //   key: 'bid',
      //   label: '招投标信息',
      //   icon: IconBid,
      //   href: '/bidding',
      // },
    ],
    [],
  )

  return (
    <>
      {/* 行业选择器：LitTrace 场景迁移后隐藏。
          它是原「行业咨询助手」的入口（智慧交通/智慧医疗/能源电力…），
          只用于切换资讯/招投标的关键词与首页行业卡片，与文献调研链路无关。
          保留代码与 store，恢复时取消注释即可。 */}
      {/* <div className="industry-selector">
        <Dropdown
          menu={{
            items: industryMenuItems,
            style: { backgroundColor: '#fff' },
          }}
          trigger={['click']}
          placement="bottomLeft"
          dropdownRender={(menu) => (
            <div
              style={{
                backgroundColor: '#fff',
                borderRadius: 8,
                boxShadow: '0 6px 16px 0 rgba(0, 0, 0, 0.08), 0 3px 6px -4px rgba(0, 0, 0, 0.12)',
                padding: 4,
              }}
            >
              {React.cloneElement(menu as React.ReactElement, {
                style: {
                  backgroundColor: '#fff',
                  boxShadow: 'none',
                },
              })}
            </div>
          )}
        >
          <div className="industry-selector__trigger">
            <span className="industry-selector__label">{currentIndustry.name}</span>
            <DownOutlined className="industry-selector__icon" />
          </div>
        </Dropdown>
      </div> */}

      <div className="base-layout-nav">
        {items.map(({ key, onClick, ...item }) => (
          <NavItem
            key={key}
            {...item}
            active={pathname === item.href}
            onClick={onClick}
          />
        ))}
      </div>
      <SessionDrawer
        open={sessionDrawerOpen}
        onClose={() => setSessionDrawerOpen(false)}
      />
    </>
  )
}
