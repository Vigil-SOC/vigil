"""
Case Notification Service - Multi-channel notification management.

Handles notifications for case events via UI, email, Slack, Teams, and PagerDuty.
"""

import logging
import re
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from core.storage.models import CaseNotification, CaseWatcher, Case
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)


class CaseNotificationService:
    """Service for managing case notifications."""
    
    def __init__(self):
        """Initialize the notification service."""
    
    def create_notification(
        self,
        user_id: str,
        notification_type: str,
        title: str,
        message: str,
        case_id: Optional[str] = None,
        delivery_channel: str = 'ui',
        priority: str = 'normal',
        metadata: Optional[Dict] = None,
        session: Optional[Session] = None
    ) -> Optional[CaseNotification]:
        """
        Create a notification for a user.
        
        Args:
            user_id: User ID to notify
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            case_id: Associated case ID
            delivery_channel: Delivery channel (ui, email, slack, teams, pagerduty)
            priority: Priority (low, normal, high, urgent)
            metadata: Additional metadata
            session: Database session (optional)
        
        Returns:
            Created CaseNotification or None
        """
        try:
            with unit_of_work(session) as session:
                notification = CaseNotification(
                    case_id=case_id,
                    user_id=user_id,
                    notification_type=notification_type,
                    title=title,
                    message=message,
                    delivery_channel=delivery_channel,
                    priority=priority,
                    metadata=metadata or {},
                    is_read=False,
                    is_sent=False
                )

                session.add(notification)

                logger.info(
                    f"Created notification for {user_id}: {notification_type}"
                )
                return notification

        except Exception as e:
            logger.error(f"Error creating notification: {e}")
            return None
    
    
    def notify_comment_mention(
        self,
        case_id: str,
        mentioned_user: str,
        comment_author: str,
        comment_content: str,
        session: Optional[Session] = None
    ) -> bool:
        """
        Notify user about being mentioned in a comment.
        
        Args:
            case_id: Case ID
            mentioned_user: User who was mentioned
            comment_author: Author of the comment
            comment_content: Comment content
            session: Database session (optional)
        
        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            case = session.query(Case).filter(Case.case_id == case_id).first()
            if not case:
                return False

            # Truncate comment for notification
            truncated_comment = (
                comment_content[:100] + '...'
                if len(comment_content) > 100 else comment_content
            )

            self.create_notification(
                user_id=mentioned_user,
                notification_type='comment_mention',
                title='Mentioned in Comment',
                message=f'{comment_author} mentioned you in case "{case.title}": {truncated_comment}',
                case_id=case_id,
                delivery_channel='ui',
                priority='normal',
                metadata={
                    'comment_author': comment_author,
                    'comment_content': comment_content
                },
                session=session
            )

            return True
    
    def notify_sla_warning(
        self,
        case_id: str,
        threshold_percent: int,
        sla_type: str,
        session: Optional[Session] = None
    ) -> bool:
        """
        Notify about approaching SLA deadline.
        
        Args:
            case_id: Case ID
            threshold_percent: Percentage of SLA elapsed
            sla_type: Type of SLA (response or resolution)
            session: Database session (optional)
        
        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            case = session.query(Case).filter(Case.case_id == case_id).first()
            if not case:
                return False

            # Notify assignee if assigned
            if case.assignee:
                urgency = 'urgent' if threshold_percent >= 90 else 'high'

                self.create_notification(
                    user_id=case.assignee,
                    notification_type='sla_warning',
                    title=f'SLA Warning: {threshold_percent}% Elapsed',
                    message=(
                        f'Case "{case.title}" has reached {threshold_percent}% '
                        f'of its {sla_type} SLA deadline'
                    ),
                    case_id=case_id,
                    delivery_channel='ui',
                    priority=urgency,
                    metadata={
                        'threshold_percent': threshold_percent,
                        'sla_type': sla_type
                    },
                    session=session
                )

            # Also notify watchers
            self.notify_watchers(
                case_id=case_id,
                notification_type='sla_warning',
                title=f'SLA Warning: {threshold_percent}%',
                message=(
                    f'Case "{case.title}" has reached {threshold_percent}% '
                    f'of its {sla_type} SLA deadline'
                ),
                session=session
            )

            return True
    
    def notify_watchers(
        self,
        case_id: str,
        notification_type: str,
        title: str,
        message: str,
        priority: str = 'normal',
        session: Optional[Session] = None
    ) -> int:
        """
        Notify all watchers of a case.
        
        Args:
            case_id: Case ID
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            priority: Priority level
            session: Database session (optional)
        
        Returns:
            Number of notifications created
        """
        with unit_of_work(session) as session:
            watchers = session.query(CaseWatcher).filter(
                CaseWatcher.case_id == case_id
            ).all()

            count = 0
            for watcher in watchers:
                # Check if user wants this type of notification
                prefs = watcher.notification_preferences or {}
                if prefs.get(notification_type, True):  # Default to True
                    self.create_notification(
                        user_id=watcher.user_id,
                        notification_type=notification_type,
                        title=title,
                        message=message,
                        case_id=case_id,
                        delivery_channel='ui',
                        priority=priority,
                        session=session
                    )
                    count += 1

            return count
    
    def extract_mentions(self, text: str) -> List[str]:
        """
        Extract @mentions from text.
        
        Args:
            text: Text to parse
        
        Returns:
            List of mentioned usernames
        """
        # Pattern: @username (alphanumeric, underscore, hyphen, period)
        pattern = r'@([\w.-]+)'
        mentions = re.findall(pattern, text)
        return list(set(mentions))  # Remove duplicates
    

