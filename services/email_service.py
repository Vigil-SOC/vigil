"""
Email Service - Send case-related emails.

Handles email notifications with HTML templates.
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional
from datetime import datetime
import os

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending emails."""
    
    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        use_tls: bool = True,
        from_address: Optional[str] = None
    ):
        """
        Initialize the email service.
        
        Args:
            smtp_host: SMTP server host
            smtp_port: SMTP server port
            smtp_user: SMTP username
            smtp_password: SMTP password
            use_tls: Use TLS encryption
            from_address: From email address
        """
        self.smtp_host = smtp_host or os.getenv('SMTP_HOST', 'localhost')
        self.smtp_port = smtp_port or int(os.getenv('SMTP_PORT', '587'))
        self.smtp_user = smtp_user or os.getenv('SMTP_USER', '')
        self.smtp_password = smtp_password or os.getenv('SMTP_PASSWORD', '')
        self.use_tls = use_tls
        self.from_address = from_address or os.getenv('SMTP_FROM', 'noreply@deeptempo.ai')
        self.enabled = all([self.smtp_host, self.smtp_user, self.smtp_password])
    
    def send_email(
        self,
        to_addresses: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        cc_addresses: Optional[List[str]] = None,
        bcc_addresses: Optional[List[str]] = None
    ) -> bool:
        """
        Send an email.
        
        Args:
            to_addresses: List of recipient email addresses
            subject: Email subject
            body_text: Plain text email body
            body_html: HTML email body (optional)
            cc_addresses: CC recipients
            bcc_addresses: BCC recipients
        
        Returns:
            True if successful
        """
        if not self.enabled:
            logger.warning("Email service not configured, skipping email")
            return False
        
        try:
            msg = MIMEMultipart('alternative')
            msg['From'] = self.from_address
            msg['To'] = ', '.join(to_addresses)
            msg['Subject'] = subject
            msg['Date'] = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S +0000')
            
            if cc_addresses:
                msg['Cc'] = ', '.join(cc_addresses)
            
            # Attach plain text version
            part1 = MIMEText(body_text, 'plain')
            msg.attach(part1)
            
            # Attach HTML version if provided
            if body_html:
                part2 = MIMEText(body_html, 'html')
                msg.attach(part2)
            
            # Combine all recipients
            all_recipients = to_addresses.copy()
            if cc_addresses:
                all_recipients.extend(cc_addresses)
            if bcc_addresses:
                all_recipients.extend(bcc_addresses)
            
            # Send email
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                if self.smtp_user and self.smtp_password:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg, self.from_address, all_recipients)
            
            logger.info(f"Email sent to {', '.join(to_addresses)}")
            return True
        
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return False
    
    def send_case_assignment_email(
        self,
        to_address: str,
        case_id: str,
        case_title: str,
        case_priority: str,
        assigned_by: Optional[str] = None
    ) -> bool:
        """
        Send case assignment notification email.
        
        Args:
            to_address: Recipient email address
            case_id: Case ID
            case_title: Case title
            case_priority: Case priority
            assigned_by: Who assigned the case
        
        Returns:
            True if successful
        """
        assigned_msg = f"by {assigned_by}" if assigned_by else "automatically"
        
        subject = f"Case Assigned: {case_title}"
        
        body_text = f"""
Case Assignment Notification

You have been assigned to the following case {assigned_msg}:

Case ID: {case_id}
Title: {case_title}
Priority: {case_priority}

Please review and take appropriate action.

--
Vigil SOC
"""
        
        body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <h2>Case Assignment Notification</h2>
    <p>You have been assigned to the following case {assigned_msg}:</p>
    
    <table style="border-collapse: collapse; margin: 20px 0;">
        <tr>
            <td style="padding: 8px; font-weight: bold;">Case ID:</td>
            <td style="padding: 8px;">{case_id}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Title:</td>
            <td style="padding: 8px;">{case_title}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Priority:</td>
            <td style="padding: 8px; text-transform: uppercase;">{case_priority}</td>
        </tr>
    </table>
    
    <p>Please review and take appropriate action.</p>
    
    <hr style="margin-top: 30px; border: none; border-top: 1px solid #ccc;">
    <p style="font-size: 12px; color: #666;">Vigil SOC</p>
</body>
</html>
"""
        
        return self.send_email(
            to_addresses=[to_address],
            subject=subject,
            body_text=body_text,
            body_html=body_html
        )
    
    def send_sla_breach_email(
        self,
        to_addresses: List[str],
        case_id: str,
        case_title: str,
        sla_type: str,
        breach_percent: int
    ) -> bool:
        """
        Send SLA breach warning email.
        
        Args:
            to_addresses: List of recipient email addresses
            case_id: Case ID
            case_title: Case title
            sla_type: SLA type (response or resolution)
            breach_percent: Percentage of SLA elapsed
        
        Returns:
            True if successful
        """
        urgency = "CRITICAL" if breach_percent >= 100 else "WARNING"
        
        subject = f"[{urgency}] SLA Alert - {case_title}"
        
        body_text = f"""
SLA Alert - {urgency}

Case {case_id} has reached {breach_percent}% of its {sla_type} SLA deadline.

Case ID: {case_id}
Title: {case_title}
SLA Type: {sla_type}
Elapsed: {breach_percent}%

Immediate attention required.

--
Vigil SOC
"""
        
        body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <div style="background-color: {'#d32f2f' if breach_percent >= 100 else '#ff9800'}; color: white; padding: 15px; border-radius: 5px;">
        <h2 style="margin: 0;">SLA Alert - {urgency}</h2>
    </div>
    
    <p style="margin-top: 20px;">Case <strong>{case_id}</strong> has reached <strong>{breach_percent}%</strong> of its <strong>{sla_type}</strong> SLA deadline.</p>
    
    <table style="border-collapse: collapse; margin: 20px 0;">
        <tr>
            <td style="padding: 8px; font-weight: bold;">Case ID:</td>
            <td style="padding: 8px;">{case_id}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Title:</td>
            <td style="padding: 8px;">{case_title}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">SLA Type:</td>
            <td style="padding: 8px; text-transform: capitalize;">{sla_type}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Elapsed:</td>
            <td style="padding: 8px;">{breach_percent}%</td>
        </tr>
    </table>
    
    <p style="font-weight: bold; color: #d32f2f;">Immediate attention required.</p>
    
    <hr style="margin-top: 30px; border: none; border-top: 1px solid #ccc;">
    <p style="font-size: 12px; color: #666;">Vigil SOC</p>
</body>
</html>
"""
        
        return self.send_email(
            to_addresses=to_addresses,
            subject=subject,
            body_text=body_text,
            body_html=body_html
        )
    
    def send_case_comment_mention_email(
        self,
        to_address: str,
        case_id: str,
        case_title: str,
        comment_author: str,
        comment_excerpt: str
    ) -> bool:
        """
        Send notification about being mentioned in a comment.
        
        Args:
            to_address: Recipient email address
            case_id: Case ID
            case_title: Case title
            comment_author: Comment author
            comment_excerpt: Excerpt of comment
        
        Returns:
            True if successful
        """
        subject = f"You were mentioned in case: {case_title}"
        
        body_text = f"""
Mention Notification

{comment_author} mentioned you in a comment on case {case_id}:

Case: {case_title}
Comment: {comment_excerpt}

--
Vigil SOC
"""
        
        body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <h2>Mention Notification</h2>
    <p><strong>{comment_author}</strong> mentioned you in a comment on case <strong>{case_id}</strong>:</p>
    
    <div style="background-color: #f5f5f5; padding: 15px; border-left: 4px solid #2196F3; margin: 20px 0;">
        <p style="margin: 0;"><strong>Case:</strong> {case_title}</p>
        <p style="margin: 10px 0 0 0;"><strong>Comment:</strong> {comment_excerpt}</p>
    </div>
    
    <hr style="margin-top: 30px; border: none; border-top: 1px solid #ccc;">
    <p style="font-size: 12px; color: #666;">Vigil SOC</p>
</body>
</html>
"""
        
        return self.send_email(
            to_addresses=[to_address],
            subject=subject,
            body_text=body_text,
            body_html=body_html
        )
    
    def send_case_escalation_email(
        self,
        to_address: str,
        case_id: str,
        case_title: str,
        escalated_from: str,
        escalation_reason: str,
        urgency_level: str
    ) -> bool:
        """
        Send case escalation notification email.
        
        Args:
            to_address: Recipient email address
            case_id: Case ID
            case_title: Case title
            escalated_from: Who escalated the case
            escalation_reason: Reason for escalation
            urgency_level: Urgency level
        
        Returns:
            True if successful
        """
        subject = f"[ESCALATION] Case Escalated: {case_title}"
        
        body_text = f"""
Case Escalation Notification

A case has been escalated to you by {escalated_from}:

Case ID: {case_id}
Title: {case_title}
Urgency: {urgency_level}
Reason: {escalation_reason}

Please review immediately.

--
Vigil SOC
"""
        
        body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <div style="background-color: #ff5722; color: white; padding: 15px; border-radius: 5px;">
        <h2 style="margin: 0;">Case Escalation Notification</h2>
    </div>
    
    <p style="margin-top: 20px;">A case has been escalated to you by <strong>{escalated_from}</strong>:</p>
    
    <table style="border-collapse: collapse; margin: 20px 0;">
        <tr>
            <td style="padding: 8px; font-weight: bold;">Case ID:</td>
            <td style="padding: 8px;">{case_id}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Title:</td>
            <td style="padding: 8px;">{case_title}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Urgency:</td>
            <td style="padding: 8px; text-transform: uppercase;">{urgency_level}</td>
        </tr>
        <tr>
            <td style="padding: 8px; font-weight: bold;">Reason:</td>
            <td style="padding: 8px;">{escalation_reason}</td>
        </tr>
    </table>
    
    <p style="font-weight: bold; color: #ff5722;">Please review immediately.</p>
    
    <hr style="margin-top: 30px; border: none; border-top: 1px solid #ccc;">
    <p style="font-size: 12px; color: #666;">Vigil SOC</p>
</body>
</html>
"""
        
        return self.send_email(
            to_addresses=[to_address],
            subject=subject,
            body_text=body_text,
            body_html=body_html
        )

