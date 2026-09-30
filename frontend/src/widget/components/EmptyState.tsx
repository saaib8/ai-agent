import type { ReactNode } from 'react'
import { Icon } from '../icons'
import type { IconName } from '../icons'

export function EmptyState({
  icon,
  title,
  text,
  action,
  onAction,
  children,
}: {
  icon: IconName
  title: string
  text: string
  action?: string
  onAction?: () => void
  children?: ReactNode
}) {
  return (
    <div className="empty">
      <span className="ring">
        <Icon name={icon} size={22} />
      </span>
      <h3>{title}</h3>
      <p>{text}</p>
      {children}
      {action && onAction && (
        <button className="btn small" onClick={onAction}>
          {action}
        </button>
      )}
    </div>
  )
}
