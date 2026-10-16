/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import * as api from '@/api'
import type { NewsItem, BiddingItem } from '@/api/news'
import ComSender, { AttachmentInfo } from '@/components/sender'
import { useQuery } from '@/router/hook'
import { deviceState } from '@/store/device'
import { industryState } from '@/store/industry'
import { setPageTransport } from '@/utils'
import { useMemo, useState, useCallback, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useSnapshot } from 'valtio'
import { message } from 'antd'
import { uniqueId } from 'lodash-es'
import dayjs from 'dayjs'
import styles from './new.module.scss'
import { transportToChatEnter } from './shared'
// LitTrace 场景迁移：行业选择器与「热门资讯 / 招投标」列表已隐藏。恢复时一并取消注释：
// import IconNews from '@/assets/layout/news.svg'
// import { message, Spin, Tag } from 'antd'
// import { ClockCircleOutlined, EnvironmentOutlined } from '@ant-design/icons'

export default function NewChat() {
  const query = useQuery()
  const navigate = useNavigate()
  const device = useSnapshot(deviceState)
  const industry = useSnapshot(industryState)

  // 获取当前行业名称
  // 场景迁移后标题为固定文案「LitTrace 文献调研助手」，该值仅用于兜底，不再显示。
  const currentIndustryName = useMemo(() => {
    const current = industry.industries.find((i: { id: string; name: string }) => i.id === industry.currentIndustryId)
    return current?.name || 'LitTrace'
  }, [industry.currentIndustryId, industry.industries])
  // 场景迁移后标题与推荐问题为固定文案；保留该值以便恢复行业维度时使用
  void currentIndustryName

  // 推荐问题 - 改为文献调研场景（原为「{行业}市场规模/主要企业/政策解读」，属行业咨询语义）
  const recommendQuestions = useMemo(() => {
    return [
      'Agent 记忆的检索粒度有哪些实证结论',
      'RAG 长上下文边界的评测方法',
      'LLM 评测中 Judge 可靠性的已有证据',
    ]
  }, [])

  // 附件状态管理
  const [attachments, setAttachments] = useState<AttachmentInfo[]>([])
  const [pendingSessionId, setPendingSessionId] = useState<string | null>(null)
  const attachmentPollingRef = useRef<NodeJS.Timeout | null>(null)

  // 热门资讯 - 从行业资讯和招投标获取，只取当月的
  type HotItem = {
    id: string
    title: string
    content?: string
    type: 'news' | 'bidding'
    date: string
    source?: string
    category?: string
    province?: string
    city?: string
    department?: string
  }
  const [hotList, setHotList] = useState<HotItem[]>([])
  const [newsLoading, setNewsLoading] = useState(true)
  // 「热门资讯」区块已隐藏（抓取也被 ENABLE_LEGACY_HOT_NEWS 关掉），
  // 这两个状态仍由保留的实现写入，显式引用以满足未使用检查。恢复区块时删除本行。
  void hotList
  void newsLoading

  // 获取最近的行业资讯和招投标（最近30天或最新数据）
  // LitTrace 场景迁移：「热门资讯」区块已隐藏，故不再发起请求。
  // 保留整个实现，把 ENABLE_LEGACY_HOT_NEWS 置 true 即可恢复（同时需恢复上方 JSX 与导入）。
  const ENABLE_LEGACY_HOT_NEWS = false
  useEffect(() => {
    if (!ENABLE_LEGACY_HOT_NEWS) return
    async function fetchHotItems() {
      try {
        setNewsLoading(true)
        const thirtyDaysAgo = dayjs().subtract(30, 'day')

        // 并行获取资讯和招投标
        const [newsRes, biddingRes] = await Promise.all([
          api.news.getNewsList({
            industry_id: industry.currentIndustryId,
            limit: 20,
            offset: 0,
          }),
          api.news.getBiddingList({
            industry_id: industry.currentIndustryId,
            limit: 20,
            offset: 0,
          }),
        ])

        const items: HotItem[] = []

        // 获取资讯（API已按时间倒序，直接取最新的）
        if (newsRes.success && newsRes.data) {
          newsRes.data.forEach((n: NewsItem) => {
            items.push({
              id: n.id,
              title: n.title,
              content: n.content,
              type: 'news',
              date: n.publish_time || n.collected_at,
              source: n.source,
              category: n.category,
              department: n.department,
            })
          })
        }

        // 获取招投标（API已按时间倒序，直接取最新的）
        if (biddingRes.success && biddingRes.data) {
          biddingRes.data.forEach((b: BiddingItem) => {
            items.push({
              id: b.id,
              title: b.title,
              content: b.content,
              type: 'bidding',
              date: b.publish_time || b.collected_at,
              source: b.source,
              category: b.notice_type,
              province: b.province,
              city: b.city,
            })
          })
        }

        // 按时间排序，取最新10条
        items.sort((a, b) => dayjs(b.date).valueOf() - dayjs(a.date).valueOf())
        setHotList(items.slice(0, 10))
      } catch (error) {
        console.error('获取热门资讯失败:', error)
        setHotList([])
      } finally {
        setNewsLoading(false)
      }
    }

    fetchHotItems()
  }, [industry.currentIndustryId])

  // 轮询检查附件处理状态
  useEffect(() => {
    // 只轮询非临时附件（已上传到服务器的）且状态为 pending/processing 的附件
    const pendingAttachments = attachments.filter(
      att => !att.id.startsWith('temp-') && (att.status === 'pending' || att.status === 'processing')
    )

    if (pendingAttachments.length > 0 && !attachmentPollingRef.current) {
      attachmentPollingRef.current = setInterval(async () => {
        for (const att of pendingAttachments) {
          try {
            const res = await api.session.getAttachment(att.id)
            const data = res.data || res
            if (data && data.status) {
              setAttachments(prev =>
                prev.map(a =>
                  a.id === att.id ? { ...a, status: data.status } : a
                )
              )
            }
          } catch (e) {
            console.error('Failed to check attachment status', e)
            // 如果获取失败，标记为完成以停止轮询
            setAttachments(prev =>
              prev.map(a =>
                a.id === att.id ? { ...a, status: 'completed' } : a
              )
            )
          }
        }
      }, 2000)
    } else if (pendingAttachments.length === 0 && attachmentPollingRef.current) {
      clearInterval(attachmentPollingRef.current)
      attachmentPollingRef.current = null
    }

    return () => {
      if (attachmentPollingRef.current) {
        clearInterval(attachmentPollingRef.current)
        attachmentPollingRef.current = null
      }
    }
  }, [attachments])

  // 上传附件
  const handleUploadAttachment = useCallback(async (file: File) => {
    // 如果还没有创建会话，先创建一个
    let sessionId = pendingSessionId
    if (!sessionId) {
      try {
        // 使用新的 session API，这样会话会出现在对话历史中
        const { data } = await api.session.createSession({ title: '新对话' })
        sessionId = data.id
        setPendingSessionId(sessionId)
      } catch (e) {
        message.error('创建会话失败')
        return null
      }
    }

    // 添加临时附件
    const tempId = uniqueId('temp-attachment-')
    setAttachments(prev => [
      ...prev,
      { id: tempId, filename: file.name, status: 'uploading' }
    ])

    try {
      const res = await api.session.uploadAttachment(sessionId, file)
      // 响应可能是 res.data 或直接是 res（取决于 axios 拦截器配置）
      const attachmentData = res.data || res
      if (attachmentData && attachmentData.id) {
        // 上传成功后，将状态设为 completed（后端处理完成后会变成 completed）
        // 如果后端返回 pending/processing，则启动轮询
        const newStatus = attachmentData.status === 'completed' ? 'completed' : attachmentData.status
        setAttachments(prev =>
          prev.map(a =>
            a.id === tempId
              ? { id: attachmentData.id, filename: attachmentData.filename, status: newStatus }
              : a
          )
        )
        message.success(`附件 ${file.name} 上传成功`)
        return attachmentData
      } else {
        // 如果没有有效响应，也标记为完成（避免一直loading）
        setAttachments(prev =>
          prev.map(a =>
            a.id === tempId
              ? { ...a, status: 'completed' }
              : a
          )
        )
      }
    } catch (e: any) {
      message.error(`附件上传失败: ${e.message || '未知错误'}`)
      setAttachments(prev => prev.filter(a => a.id !== tempId))
    }
    return null
  }, [pendingSessionId])

  // 移除附件
  const handleRemoveAttachment = useCallback(async (attachmentId: string) => {
    try {
      if (!attachmentId.startsWith('temp-')) {
        await api.session.deleteAttachment(attachmentId)
      }
      setAttachments(prev => prev.filter(a => a.id !== attachmentId))
    } catch (e) {
      console.error('Failed to delete attachment', e)
    }
  }, [])

  /**
   * 推荐问题：**只建立会话 + 预填问题**，由用户确认后再发送。
   *
   * 为什么不能直接 `send(question)`：旧实现进会话页后会自动发送，而「搜索模式」默认是空的，
   * 于是走到已下线的普通对话接口 `/chat/completion`（404，静默失败）。
   * 现在推荐入口不触发任何研究请求，用户可在表单里确认方向/窗口后统一走 `/research/stream`。
   */
  async function startFromRecommendation(question: string) {
    try {
      const title = question.length > 20 ? question.slice(0, 20) + '...' : question
      const { data } = await api.session.createSession({ title })
      setPageTransport(transportToChatEnter, {
        data: { prefill: question, attachmentIds: undefined },
      })
      navigate(`/chat/${data.id}`)
    } catch (e) {
      message.error('创建研究会话失败，请重试')
    }
  }

  async function send(msg: string, attachmentIds?: string[]) {
    // 如果已经有预创建的会话（因为上传了附件），直接使用它
    let sessionId = pendingSessionId
    if (!sessionId) {
      // 使用新的 session API，这样会话会出现在对话历史中
      // 用问题的前20个字符作为标题
      const title = msg.length > 20 ? msg.slice(0, 20) + '...' : msg
      const { data } = await api.session.createSession({ title })
      sessionId = data.id
    } else {
      // 如果已有会话，更新标题
      try {
        const title = msg.length > 20 ? msg.slice(0, 20) + '...' : msg
        await api.session.updateSession(sessionId, { title })
      } catch (e) {
        console.error('更新会话标题失败', e)
      }
    }

    setPageTransport(transportToChatEnter, {
      data: {
        message: msg,
        attachmentIds,
      },
    })
    navigate(`/chat/${sessionId}`)
  }

  return (
    <div className={styles['newchat-page']}>
      <div className={styles['newchat-page__header']}>
        {query.get('title') || 'LitTrace 文献调研助手'}
      </div>

      <ComSender
        className={styles['newchat-page__sender']}
        attachments={attachments}
        onSend={send}
        onUploadAttachment={handleUploadAttachment}
        onRemoveAttachment={handleRemoveAttachment}
      />

      {/* 推荐问题 - 和标签在同一行 */}
      <div className={styles['newchat-page__questions-row']}>
        <span className={styles['questions-label']}>推荐问题</span>
        <div className={styles['questions-list']}>
          {recommendQuestions.map((question, index) => (
            <span
              className={styles['question-tag']}
              key={index}
              onClick={() => startFromRecommendation(question)}
            >
              {question}
            </span>
          ))}
        </div>
      </div>

      {/* 热门资讯：LitTrace 场景迁移后隐藏。
          来自已被隐藏的「行业资讯 / 招投标」接口，与文献调研无关。
          保留代码与 hotList 抓取逻辑，恢复时取消注释即可。 */}
      {/*
      <div className={styles['newchat-page__section']}>
        <div className={styles['section-header']}>
          <div className={styles['section-icon']}>
            <img src={IconNews} />
          </div>
          <span className={styles['section-title']}>热门资讯</span>
        </div>
        <div className={styles['newchat-page__news-list']}>
          {newsLoading ? (
            <div style={{ padding: '20px', textAlign: 'center' }}>
              <Spin size="small" />
            </div>
          ) : hotList.length > 0 ? (
            hotList.map((item) => (
              <div
                className={styles['newchat-page__news-card']}
                key={item.id}
                onClick={() => send(`请帮我分析：${item.title}`)}
              >
                <div className={styles['news-card__title']}>{item.title}</div>
                <div className={styles['news-card__info']}>
                  <Tag color="blue">
                    {item.type === 'news'
                      ? (item.category || '资讯')
                      : `招标 | ${item.category || '招标公告'}`}
                  </Tag>
                  {item.type === 'bidding' && (item.province || item.city) && (
                    <span className={styles['info-location']}>
                      <EnvironmentOutlined />
                      {[item.province, item.city].filter(Boolean).join(' ')}
                    </span>
                  )}
                  {item.type === 'news' && item.department && (
                    <span className={styles['info-location']}>
                      <EnvironmentOutlined />
                      {item.department}
                    </span>
                  )}
                </div>
                <div className={styles['news-card__meta']}>
                  <span className={styles['meta-item']}>
                    <ClockCircleOutlined />
                    发布于 {dayjs(item.date).format('YYYY/MM/DD')}
                  </span>
                  <span className={styles['meta-item']}>
                    ID: {item.id.slice(0, 8)}...
                  </span>
                </div>
              </div>
            ))
          ) : (
            <div style={{ padding: '20px', textAlign: 'center', color: '#bfbfbf', fontSize: 13 }}>
              暂无资讯，请点击"行业资讯"页面采集
            </div>
          )}
        </div>
      </div>
      */}
    </div>
  )
}
