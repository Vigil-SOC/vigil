"""
Case Collaboration Service - Comments, watchers, and collaboration.

Handles case comments, @mentions, watchers, and activity feeds.
"""

import logging
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_

from core.storage.models import CaseComment, CaseWatcher
from core.cases.case_notification_service import CaseNotificationService
from core.storage.unit_of_work import unit_of_work
from core.exceptions import default_on_error

logger = logging.getLogger(__name__)


class CaseCollaborationService:
    """Service for managing case collaboration."""

    def __init__(self):
        """Initialize the collaboration service."""
        self.notification_service = CaseNotificationService()

    @default_on_error(None)
    def add_comment(
        self,
        case_id: str,
        author: str,
        content: str,
        parent_comment_id: Optional[int] = None,
        session: Optional[Session] = None
    ) -> Optional[CaseComment]:
        """
        Add a comment to a case.

        Args:
            case_id: Case ID
            author: Comment author
            content: Comment content
            parent_comment_id: Parent comment ID for threading
            session: Database session (optional)

        Returns:
            Created CaseComment or None
        """
        with unit_of_work(session) as session:
            # Extract mentions from content
            mentions = self.notification_service.extract_mentions(content)

            comment = CaseComment(
                case_id=case_id,
                parent_comment_id=parent_comment_id,
                author=author,
                content=content,
                mentions=mentions,
                is_edited=False,
                is_deleted=False
            )

            session.add(comment)

            # Send notifications for mentions
            for mentioned_user in mentions:
                if mentioned_user != author:  # Don't notify self
                    self.notification_service.notify_comment_mention(
                        case_id=case_id,
                        mentioned_user=mentioned_user,
                        comment_author=author,
                        comment_content=content,
                        session=session
                    )

            # Notify watchers
            self.notification_service.notify_watchers(
                case_id=case_id,
                notification_type='new_comment',
                title='New Comment',
                message=f'{author} added a comment',
                session=session
            )

            logger.info(f"Added comment to case {case_id} by {author}")
            return comment


    @default_on_error(False)
    def update_comment(
        self,
        comment_id: int,
        new_content: str,
        session: Optional[Session] = None
    ) -> bool:
        """
        Update a comment.

        Args:
            comment_id: Comment ID
            new_content: New comment content
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            comment = session.query(CaseComment).filter(
                CaseComment.comment_id == comment_id
            ).first()

            if not comment:
                return False

            comment.content = new_content
            comment.is_edited = True

            # Update mentions
            mentions = self.notification_service.extract_mentions(new_content)
            comment.mentions = mentions

            return True


    @default_on_error(False)
    def delete_comment(
        self,
        comment_id: int,
        soft_delete: bool = True,
        session: Optional[Session] = None
    ) -> bool:
        """
        Delete a comment.

        Args:
            comment_id: Comment ID
            soft_delete: If True, mark as deleted; if False, hard delete
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            comment = session.query(CaseComment).filter(
                CaseComment.comment_id == comment_id
            ).first()

            if not comment:
                return False

            if soft_delete:
                comment.is_deleted = True
                comment.content = "[deleted]"
            else:
                session.delete(comment)

            return True


    def get_case_comments(
        self,
        case_id: str,
        include_deleted: bool = False,
        session: Optional[Session] = None
    ) -> List[CaseComment]:
        """
        Get all comments for a case.

        Args:
            case_id: Case ID
            include_deleted: Include deleted comments
            session: Database session (optional)

        Returns:
            List of CaseComment objects
        """
        with unit_of_work(session) as session:
            query = session.query(CaseComment).filter(
                CaseComment.case_id == case_id
            )

            if not include_deleted:
                query = query.filter(CaseComment.is_deleted == False)

            return query.order_by(CaseComment.created_at.asc()).all()

    @default_on_error(None)
    def add_watcher(
        self,
        case_id: str,
        user_id: str,
        notification_preferences: Optional[Dict] = None,
        session: Optional[Session] = None
    ) -> Optional[CaseWatcher]:
        """
        Add a watcher to a case.

        Args:
            case_id: Case ID
            user_id: User ID to add as watcher
            notification_preferences: Notification preferences
            session: Database session (optional)

        Returns:
            Created CaseWatcher or None
        """
        with unit_of_work(session) as session:
            # Check if already watching
            existing = session.query(CaseWatcher).filter(
                and_(
                    CaseWatcher.case_id == case_id,
                    CaseWatcher.user_id == user_id
                )
            ).first()

            if existing:
                logger.info(f"User {user_id} already watching case {case_id}")
                return existing

            watcher = CaseWatcher(
                case_id=case_id,
                user_id=user_id,
                notification_preferences=notification_preferences or {}
            )

            session.add(watcher)

            logger.info(f"Added watcher {user_id} to case {case_id}")
            return watcher


    @default_on_error(False)
    def remove_watcher(
        self,
        case_id: str,
        user_id: str,
        session: Optional[Session] = None
    ) -> bool:
        """
        Remove a watcher from a case.

        Args:
            case_id: Case ID
            user_id: User ID to remove
            session: Database session (optional)

        Returns:
            True if successful
        """
        with unit_of_work(session) as session:
            watcher = session.query(CaseWatcher).filter(
                and_(
                    CaseWatcher.case_id == case_id,
                    CaseWatcher.user_id == user_id
                )
            ).first()

            if not watcher:
                return False

            session.delete(watcher)

            logger.info(f"Removed watcher {user_id} from case {case_id}")
            return True


    def get_case_watchers(
        self,
        case_id: str,
        session: Optional[Session] = None
    ) -> List[CaseWatcher]:
        """
        Get all watchers for a case.

        Args:
            case_id: Case ID
            session: Database session (optional)

        Returns:
            List of CaseWatcher objects
        """
        with unit_of_work(session) as session:
            return session.query(CaseWatcher).filter(
                CaseWatcher.case_id == case_id
            ).all()
