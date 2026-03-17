"""PDF report generation service."""

import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

logger = logging.getLogger(__name__)


class ReportService:
    """Service for generating PDF reports."""
    
    def __init__(self):
        """Initialize the report service."""
        if not REPORTLAB_AVAILABLE:
            raise ImportError(
                "reportlab is not installed. Please install it with: pip install reportlab"
            )
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
    
    def _setup_custom_styles(self):
        """Set up custom paragraph styles."""
        # Title style
        self.styles.add(ParagraphStyle(
            name='CustomTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1a1a1a'),
            spaceAfter=30,
            alignment=TA_CENTER
        ))
        
        # Heading style
        self.styles.add(ParagraphStyle(
            name='CustomHeading',
            parent=self.styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor('#2c3e50'),
            spaceAfter=12,
            spaceBefore=12
        ))
        
        # Subheading style
        self.styles.add(ParagraphStyle(
            name='CustomSubheading',
            parent=self.styles['Heading3'],
            fontSize=12,
            textColor=colors.HexColor('#34495e'),
            spaceAfter=8,
            spaceBefore=8
        ))
        
        # Body text style
        self.styles.add(ParagraphStyle(
            name='CustomBody',
            parent=self.styles['Normal'],
            fontSize=10,
            textColor=colors.HexColor('#333333'),
            spaceAfter=6
        ))
    
    def generate_overall_report(self, output_path: Path, findings: List[Dict], 
                               cases: List[Dict], include_findings_detail: bool = True) -> bool:
        """
        Generate an overall SOC report PDF.
        
        Args:
            output_path: Path to save the PDF file.
            findings: List of all findings.
            cases: List of all cases.
            include_findings_detail: Whether to include detailed findings section.
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            doc = SimpleDocTemplate(str(output_path), pagesize=letter,
                                   rightMargin=72, leftMargin=72,
                                   topMargin=72, bottomMargin=18)
            story = []
            
            # Title
            story.append(Paragraph("Vigil SOC - Overall Report", self.styles['CustomTitle']))
            story.append(Spacer(1, 0.2*inch))
            
            # Report metadata
            report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            metadata = [
                ["Report Generated:", report_date],
                ["Total Findings:", str(len(findings))],
                ["Total Cases:", str(len(cases))],
            ]
            metadata_table = Table(metadata, colWidths=[2*inch, 4*inch])
            metadata_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#ecf0f1')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
            ]))
            story.append(metadata_table)
            story.append(Spacer(1, 0.3*inch))
            
            # Executive Summary
            story.append(Paragraph("Executive Summary", self.styles['CustomHeading']))
            
            # Findings by severity
            severity_counts = {}
            for finding in findings:
                severity = finding.get('severity', 'unknown')
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
            
            summary_data = [["Severity", "Count", "Percentage"]]
            total = len(findings)
            for severity in ["critical", "high", "medium", "low"]:
                count = severity_counts.get(severity, 0)
                percentage = (count / total * 100) if total > 0 else 0
                summary_data.append([severity.capitalize(), str(count), f"{percentage:.1f}%"])
            
            summary_table = Table(summary_data, colWidths=[2*inch, 1.5*inch, 1.5*inch])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3498db')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ]))
            story.append(summary_table)
            story.append(Spacer(1, 0.2*inch))
            
            # Cases summary
            story.append(Paragraph("Cases Summary", self.styles['CustomHeading']))
            
            cases_data = [["Case ID", "Title", "Status", "Priority", "Findings"]]
            for case in cases[:20]:  # Limit to first 20 cases
                cases_data.append([
                    case.get('case_id', 'N/A'),
                    case.get('title', 'N/A')[:40] + ('...' if len(case.get('title', '')) > 40 else ''),
                    case.get('status', 'N/A'),
                    case.get('priority', 'N/A'),
                    str(len(case.get('finding_ids', [])))
                ])
            
            if len(cases) > 20:
                cases_data.append(["", f"... and {len(cases) - 20} more cases", "", "", ""])
            
            cases_table = Table(cases_data, colWidths=[1.5*inch, 2.5*inch, 1*inch, 1*inch, 1*inch])
            cases_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2ecc71')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
            ]))
            story.append(cases_table)
            story.append(PageBreak())
            
            # Findings detail (if requested)
            if include_findings_detail and findings:
                story.append(Paragraph("Findings Detail", self.styles['CustomHeading']))
                
                for i, finding in enumerate(findings[:50], 1):  # Limit to first 50 findings
                    story.append(Paragraph(f"Finding {i}: {finding.get('finding_id', 'N/A')}", 
                                          self.styles['CustomSubheading']))
                    
                    finding_info = [
                        ["Field", "Value"],
                        ["Finding ID", finding.get('finding_id', 'N/A')],
                        ["Timestamp", finding.get('timestamp', 'N/A')],
                        ["Severity", finding.get('severity', 'N/A')],
                        ["Data Source", finding.get('data_source', 'N/A')],
                        ["Anomaly Score", f"{finding.get('anomaly_score', 0):.3f}"],
                        ["Cluster ID", finding.get('cluster_id', 'None')],
                    ]
                    
                    # Entity context
                    entity_context = finding.get('entity_context') or {}
                    if entity_context:
                        for key, value in list(entity_context.items())[:5]:  # Limit fields
                            finding_info.append([f"Entity: {key}", str(value)])
                    
                    # MITRE techniques
                    mitre_preds = finding.get('mitre_predictions') or {}
                    if mitre_preds:
                        top_techniques = sorted(mitre_preds.items(), key=lambda x: float(x[1] or 0), reverse=True)[:5]
                        tech_str = ", ".join([f"{t[0]} ({float(t[1] or 0):.2f})" for t in top_techniques])
                        finding_info.append(["MITRE Techniques", tech_str])
                    
                    finding_table = Table(finding_info, colWidths=[2*inch, 4*inch])
                    finding_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#95a5a6')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, 0), 10),
                        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                        ('FONTSIZE', (0, 1), (-1, -1), 9),
                    ]))
                    story.append(finding_table)
                    story.append(Spacer(1, 0.1*inch))
                    
                    if i % 10 == 0:  # Page break every 10 findings
                        story.append(PageBreak())
            
            # Build PDF
            doc.build(story)
            logger.info(f"Generated overall report: {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"Error generating overall report: {e}", exc_info=True)
            return False
    
    def generate_case_report(self, output_path: Path, case: Dict, findings: List[Dict]) -> bool:
        """
        Generate a PDF report for a specific case.
        
        Args:
            output_path: Path to save the PDF file.
            case: Case dictionary.
            findings: List of findings related to the case.
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            doc = SimpleDocTemplate(str(output_path), pagesize=letter,
                                   rightMargin=72, leftMargin=72,
                                   topMargin=72, bottomMargin=18)
            story = []
            
            # Title
            title = case.get('title', 'Investigation Case')
            story.append(Paragraph(f"Case Report: {title}", self.styles['CustomTitle']))
            story.append(Spacer(1, 0.2*inch))
            
            # Case metadata
            case_id = case.get('case_id', 'N/A')
            created = case.get('created_at', 'N/A')
            updated = case.get('updated_at', 'N/A')
            status = case.get('status', 'N/A')
            priority = case.get('priority', 'N/A')
            assignee = case.get('assignee', 'Unassigned')
            
            metadata = [
                ["Case ID:", case_id],
                ["Status:", status.capitalize()],
                ["Priority:", priority.capitalize()],
                ["Assignee:", assignee],
                ["Created:", created.split('T')[0] if 'T' in created else created],
                ["Last Updated:", updated.split('T')[0] if 'T' in updated else updated],
            ]
            
            metadata_table = Table(metadata, colWidths=[2*inch, 4*inch])
            metadata_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#ecf0f1')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                ('ALIGN', (1, 0), (1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ]))
            story.append(metadata_table)
            story.append(Spacer(1, 0.3*inch))
            
            # Description
            description = case.get('description', 'No description provided.')
            story.append(Paragraph("Description", self.styles['CustomHeading']))
            story.append(Paragraph(description, self.styles['CustomBody']))
            story.append(Spacer(1, 0.2*inch))
            
            # Tags
            tags = case.get('tags', [])
            if tags:
                story.append(Paragraph("Tags", self.styles['CustomHeading']))
                tags_text = ", ".join(tags)
                story.append(Paragraph(tags_text, self.styles['CustomBody']))
                story.append(Spacer(1, 0.2*inch))
            
            # Timeline
            timeline = case.get('timeline', [])
            if timeline:
                story.append(Paragraph("Timeline", self.styles['CustomHeading']))
                timeline_data = [["Timestamp", "Event"]]
                for event in timeline:
                    timestamp = event.get('timestamp', 'N/A')
                    if 'T' in timestamp:
                        timestamp = timestamp.split('T')[0] + ' ' + timestamp.split('T')[1].split('.')[0]
                    timeline_data.append([timestamp, event.get('event', 'N/A')])
                
                timeline_table = Table(timeline_data, colWidths=[2.5*inch, 3.5*inch])
                timeline_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3498db')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                ]))
                story.append(timeline_table)
                story.append(Spacer(1, 0.2*inch))
            
            # Notes
            notes = case.get('notes', [])
            if notes:
                story.append(Paragraph("Investigation Notes", self.styles['CustomHeading']))
                for note in notes:
                    note_text = note.get('text', note.get('content', 'N/A'))
                    note_timestamp = note.get('timestamp', 'N/A')
                    if 'T' in note_timestamp:
                        note_timestamp = note_timestamp.split('T')[0]
                    story.append(Paragraph(f"<b>{note_timestamp}:</b> {note_text}", self.styles['CustomBody']))
                    story.append(Spacer(1, 0.1*inch))
                story.append(Spacer(1, 0.2*inch))
            
            # Activities
            activities = case.get('activities', [])
            if activities:
                story.append(Paragraph("Case Activities", self.styles['CustomHeading']))
                activities_data = [["Timestamp", "Type", "Description"]]
                for activity in activities:
                    timestamp = activity.get('timestamp', 'N/A')
                    if 'T' in timestamp:
                        timestamp = timestamp.split('T')[0] + ' ' + timestamp.split('T')[1].split('.')[0]
                    activity_type = activity.get('activity_type', 'N/A').replace('_', ' ').title()
                    description = activity.get('description', 'N/A')[:100]
                    activities_data.append([timestamp, activity_type, description])
                
                activities_table = Table(activities_data, colWidths=[2*inch, 1.5*inch, 2.5*inch])
                activities_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#9b59b6')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                    ('FONTSIZE', (0, 1), (-1, -1), 9),
                ]))
                story.append(activities_table)
                story.append(Spacer(1, 0.2*inch))
            
            # Resolution Steps
            resolution_steps = case.get('resolution_steps', [])
            if resolution_steps:
                story.append(Paragraph("Resolution Steps", self.styles['CustomHeading']))
                for i, step in enumerate(resolution_steps, 1):
                    story.append(Paragraph(f"Step {i}: {step.get('description', 'N/A')}", 
                                          self.styles['CustomSubheading']))
                    
                    step_info = [
                        ["Field", "Value"],
                        ["Timestamp", step.get('timestamp', 'N/A')],
                        ["Action Taken", step.get('action_taken', 'N/A')],
                    ]
                    
                    result = step.get('result', '')
                    if result:
                        step_info.append(["Result", result])
                    
                    step_table = Table(step_info, colWidths=[2*inch, 4*inch])
                    step_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#27ae60')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, 0), 10),
                        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                        ('FONTSIZE', (0, 1), (-1, -1), 9),
                    ]))
                    story.append(step_table)
                    story.append(Spacer(1, 0.15*inch))
                story.append(Spacer(1, 0.2*inch))
            
            # Findings
            story.append(Paragraph(f"Related Findings ({len(findings)})", self.styles['CustomHeading']))
            
            if findings:
                findings_data = [["Finding ID", "Severity", "Data Source", "Anomaly Score", "MITRE Techniques"]]
                for finding in findings:
                    finding_id = finding.get('finding_id', 'N/A')
                    severity = finding.get('severity', 'N/A')
                    data_source = finding.get('data_source', 'N/A')
                    anomaly_score = f"{float(finding.get('anomaly_score') or 0):.3f}"
                    
                    # Get top MITRE techniques
                    mitre_preds = finding.get('mitre_predictions') or {}
                    if mitre_preds:
                        top_techs = sorted(mitre_preds.items(), key=lambda x: float(x[1] or 0), reverse=True)[:3]
                        tech_str = ", ".join([t[0] for t in top_techs])
                    else:
                        tech_str = "None"
                    
                    findings_data.append([finding_id, severity, data_source, anomaly_score, tech_str])
                
                findings_table = Table(findings_data, colWidths=[1.5*inch, 1*inch, 1*inch, 1*inch, 1.5*inch])
                findings_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e74c3c')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                    ('FONTSIZE', (0, 1), (-1, -1), 8),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
                ]))
                story.append(findings_table)
                story.append(PageBreak())
                
                # Detailed findings
                story.append(Paragraph("Finding Details", self.styles['CustomHeading']))
                
                for i, finding in enumerate(findings, 1):
                    story.append(Paragraph(f"Finding {i}: {finding.get('finding_id', 'N/A')}", 
                                          self.styles['CustomSubheading']))
                    
                    finding_info = [
                        ["Field", "Value"],
                        ["Finding ID", finding.get('finding_id', 'N/A')],
                        ["Timestamp", finding.get('timestamp', 'N/A')],
                        ["Severity", finding.get('severity', 'N/A')],
                        ["Data Source", finding.get('data_source', 'N/A')],
                        ["Anomaly Score", f"{finding.get('anomaly_score', 0):.3f}"],
                        ["Cluster ID", finding.get('cluster_id', 'None')],
                    ]
                    
                    # Entity context
                    entity_context = finding.get('entity_context') or {}
                    if entity_context:
                        for key, value in entity_context.items():
                            finding_info.append([f"Entity: {key}", str(value)])
                    
                    # MITRE techniques
                    mitre_preds = finding.get('mitre_predictions') or {}
                    if mitre_preds:
                        for tech_id, confidence in sorted(mitre_preds.items(), key=lambda x: float(x[1] or 0), reverse=True)[:10]:
                            finding_info.append([f"MITRE: {tech_id}", f"{float(confidence or 0):.3f}"])
                    
                    finding_table = Table(finding_info, colWidths=[2*inch, 4*inch])
                    finding_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#95a5a6')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, 0), 9),
                        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                        ('FONTSIZE', (0, 1), (-1, -1), 8),
                    ]))
                    story.append(finding_table)
                    story.append(Spacer(1, 0.1*inch))
            else:
                story.append(Paragraph("No findings associated with this case.", self.styles['CustomBody']))
            
            # Build PDF
            doc.build(story)
            logger.info(f"Generated case report: {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"Error generating case report: {e}", exc_info=True)
            return False
    
    def generate_investigation_chat_report(self, output_path: Path, tab_title: str,
                                          conversation_history: List[Dict], 
                                          focused_findings: List[Dict] = None,
                                          notes: str = None) -> bool:
        """
        Generate a PDF report for an investigation chat/conversation.
        
        Args:
            output_path: Path to save the PDF file.
            tab_title: Title of the investigation tab.
            conversation_history: List of conversation messages (role, content).
            focused_findings: Optional list of findings being investigated.
            notes: Optional investigation notes.
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            doc = SimpleDocTemplate(str(output_path), pagesize=letter,
                                   rightMargin=72, leftMargin=72,
                                   topMargin=72, bottomMargin=18)
            story = []
            
            # Title
            story.append(Paragraph(f"Investigation Report: {tab_title}", self.styles['CustomTitle']))
            story.append(Spacer(1, 0.2*inch))
            
            # Report metadata
            report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            metadata = [
                ["Report Generated:", report_date],
                ["Investigation:", tab_title],
                ["Total Messages:", str(len(conversation_history))],
            ]
            
            if focused_findings:
                metadata.append(["Focused Findings:", str(len(focused_findings))])
            
            metadata_table = Table(metadata, colWidths=[2*inch, 4*inch])
            metadata_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#ecf0f1')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ]))
            story.append(metadata_table)
            story.append(Spacer(1, 0.3*inch))
            
            # Investigation Notes
            if notes and notes.strip():
                story.append(Paragraph("Investigation Notes", self.styles['CustomHeading']))
                # Escape HTML entities in notes
                notes_escaped = notes.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(notes_escaped, self.styles['CustomBody']))
                story.append(Spacer(1, 0.2*inch))
            
            # Focused Findings Summary
            if focused_findings:
                story.append(Paragraph(f"Focused Findings ({len(focused_findings)})", 
                                      self.styles['CustomHeading']))
                
                findings_data = [["Finding ID", "Severity", "Data Source", "Anomaly Score"]]
                for finding in focused_findings:
                    findings_data.append([
                        finding.get('finding_id', 'N/A'),
                        finding.get('severity', 'N/A'),
                        finding.get('data_source', 'N/A'),
                        f"{finding.get('anomaly_score', 0):.3f}"
                    ])
                
                findings_table = Table(findings_data, colWidths=[2*inch, 1.5*inch, 1.5*inch, 1*inch])
                findings_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e74c3c')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                    ('FONTSIZE', (0, 1), (-1, -1), 9),
                ]))
                story.append(findings_table)
                story.append(Spacer(1, 0.2*inch))
            
            # Chat Conversation
            story.append(Paragraph("Chat Conversation", self.styles['CustomHeading']))
            story.append(Spacer(1, 0.1*inch))
            
            for i, message in enumerate(conversation_history, 1):
                role = message.get('role', 'unknown')
                content = message.get('content', '')
                
                # Escape HTML entities in content
                content_escaped = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                
                # Truncate very long messages
                if len(content_escaped) > 5000:
                    content_escaped = content_escaped[:5000] + "\n\n[... message truncated for PDF ...]"
                
                # Style by role
                if role == 'user':
                    role_color = '#2196F3'
                    role_label = '👤 User'
                elif role == 'assistant':
                    role_color = '#4CAF50'
                    role_label = '🤖 Assistant'
                else:
                    role_color = '#9E9E9E'
                    role_label = role.capitalize()
                
                # Message header
                story.append(Paragraph(
                    f"<b><font color='{role_color}'>{role_label} (Message {i})</font></b>",
                    self.styles['CustomSubheading']
                ))
                
                # Message content - split into paragraphs for better formatting
                paragraphs = content_escaped.split('\n\n')
                for para in paragraphs:
                    if para.strip():
                        # Replace single newlines with spaces
                        para_clean = para.replace('\n', ' ').strip()
                        story.append(Paragraph(para_clean, self.styles['CustomBody']))
                
                story.append(Spacer(1, 0.15*inch))
                
                # Page break every 5 messages to avoid overly long pages
                if i % 5 == 0 and i < len(conversation_history):
                    story.append(PageBreak())
            
            # Build PDF
            doc.build(story)
            logger.info(f"Generated investigation chat report: {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"Error generating investigation chat report: {e}", exc_info=True)
            return False

