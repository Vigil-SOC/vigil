#!/usr/bin/env python3
"""
Demonstration of Chat-Driven Case Management

This script demonstrates the new chat-driven case management capabilities
where Claude can automatically build out cases based on natural language prompts.

Run this script to see example interactions and test the functionality.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import asyncio
from core.llm.harness.claude import ClaudeService
from core.storage.database_data_service import DatabaseDataService


def print_section(title: str):
    """Print a formatted section header."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def print_result(label: str, data: any):
    """Print formatted result."""
    print(f"\n{label}:")
    if isinstance(data, dict) or isinstance(data, list):
        print(json.dumps(data, indent=2))
    else:
        print(data)
    print()


async def demo_case_building():
    """Demonstrate building a case through chat."""
    
    print_section("Chat-Driven Case Management Demo")
    
    # Initialize services
    claude = ClaudeService(use_mcp_tools=True)
    data_service = DatabaseDataService()
    
    # Check if Claude is configured
    if not claude.has_api_key():
        print("❌ Claude API key not configured. Please set CLAUDE_API_KEY.")
        print("   Use: python3 backend/api/config.py set claude api_key YOUR_KEY")
        return
    
    print("✅ Claude API configured")
    print("✅ MCP tools loaded")
    print(f"✅ Using data backend: {data_service.get_backend_info()['backend']}")
    
    # Get some sample findings for the demo
    print_section("Step 1: Finding Sample Data")
    findings = data_service.get_findings(limit=5)
    
    if not findings:
        print("❌ No findings available. Generate sample data first:")
        print("   python3 scripts/generate_sample_data.py")
        return
    
    print(f"✅ Found {len(findings)} sample findings")
    for f in findings[:3]:
        print(f"   - {f['finding_id']}: {f.get('severity', 'unknown')} severity")
    
    # Demo 1: Simple case creation
    print_section("Demo 1: Creating a Case from Chat")
    
    prompt1 = f"""I've identified a security incident. The finding {findings[0]['finding_id']} 
shows suspicious lateral movement. Can you create a case called 'Lateral Movement Investigation' 
and add this finding to it?"""
    
    print(f"User: {prompt1}\n")
    print("Claude: Processing...")
    
    try:
        response1 = claude.chat(
            message=prompt1,
            model="claude-sonnet-4-20250514",
            max_tokens=4096
        )
        print_result("Claude's Response", response1)
    except Exception as e:
        print(f"❌ Error: {e}")
        return
    
    # Demo 2: Adding multiple findings
    print_section("Demo 2: Bulk Adding Findings")
    
    # Get the case that was just created (would need to parse response)
    cases = data_service.get_cases(limit=1)
    if not cases:
        print("⚠️  No case was created. The demo may need Claude API access.")
        return
    
    case_id = cases[0]['case_id']
    
    if len(findings) >= 3:
        finding_ids = [f['finding_id'] for f in findings[1:4]]
        prompt2 = f"""I've found more related findings: {', '.join(finding_ids)}. 
These all show the same attack pattern - RDP lateral movement. Can you add them to 
case {case_id} and note that they're part of the same campaign?"""
        
        print(f"User: {prompt2}\n")
        print("Claude: Processing...")
        
        try:
            response2 = claude.chat(
                message=prompt2,
                model="claude-sonnet-4-20250514",
                max_tokens=4096
            )
            print_result("Claude's Response", response2)
        except Exception as e:
            print(f"❌ Error: {e}")
    
    # Demo 3: Building timeline
    print_section("Demo 3: Building Attack Timeline")
    
    prompt3 = f"""For case {case_id}, can you add timeline entries documenting the attack progression:
1. Initial access at 2026-01-21 09:00 UTC via compromised credentials (T1078)
2. Lateral movement at 09:15 UTC using RDP (T1021.001)
3. Credential dumping at 09:30 UTC (T1003.001)

Also tag these MITRE techniques."""
    
    print(f"User: {prompt3}\n")
    print("Claude: Processing...")
    
    try:
        response3 = claude.chat(
            message=prompt3,
            model="claude-sonnet-4-20250514",
            max_tokens=4096
        )
        print_result("Claude's Response", response3)
    except Exception as e:
        print(f"❌ Error: {e}")
    
    # Demo 4: Adding resolution steps
    print_section("Demo 4: Documenting Resolution")
    
    prompt4 = f"""For case {case_id}, I've taken the following actions:
1. Isolated the affected systems from the network
2. Reset passwords for compromised accounts
3. Blocked the attacker's source IP at the firewall

Can you log these as resolution steps?"""
    
    print(f"User: {prompt4}\n")
    print("Claude: Processing...")
    
    try:
        response4 = claude.chat(
            message=prompt4,
            model="claude-sonnet-4-20250514",
            max_tokens=4096
        )
        print_result("Claude's Response", response4)
    except Exception as e:
        print(f"❌ Error: {e}")
    
    # Show final case state
    print_section("Final Case State")
    
    final_case = data_service.get_case(case_id)
    if final_case:
        print("Case Summary:")
        print(f"  ID: {final_case['case_id']}")
        print(f"  Title: {final_case['title']}")
        print(f"  Status: {final_case['status']}")
        print(f"  Priority: {final_case['priority']}")
        print(f"  Findings: {len(final_case.get('finding_ids', []))}")
        print(f"  Activities: {len(final_case.get('activities', []))}")
        print(f"  Timeline Entries: {len(final_case.get('timeline', []))}")
        print(f"  MITRE Techniques: {', '.join(final_case.get('mitre_techniques', []))}")
        print(f"  Resolution Steps: {len(final_case.get('resolution_steps', []))}")
    
    print_section("Demo Complete")
    
    print("""
✅ The demo showed how Claude can automatically:
   - Create cases from natural language requests
   - Add findings to cases (single and bulk)
   - Build attack timelines with MITRE mapping
   - Document resolution steps
   - Log all investigation activities

Try it yourself in the chat interface!

Example prompts to try:
- "Show me critical findings from the last week"
- "Analyze finding f-20260121-abc123"
- "This looks like part of an APT campaign - create a case"
- "Add findings f-001, f-002, f-003 to the case"
- "Note that this is T1071 C2 communication"
- "Log that I've isolated the affected systems"
""")


def demo_direct_mcp():
    """Demonstrate direct MCP tool usage (without chat)."""
    
    print_section("Direct MCP Tool Usage (Advanced)")
    
    print("""
The MCP tools can also be called directly via the MCP server.
Available tools:

Case Management:
  - deeptempo-findings_create_case
  - deeptempo-findings_add_finding_to_case
  - deeptempo-findings_bulk_add_findings_to_case
  - deeptempo-findings_add_case_activity
  - deeptempo-findings_add_case_timeline_entry
  - deeptempo-findings_add_case_mitre_techniques
  - deeptempo-findings_add_resolution_step
  - deeptempo-findings_create_case_from_killchain
  - deeptempo-findings_update_case
  - deeptempo-findings_get_case
  - deeptempo-findings_list_cases

Example tool call (via MCP):
{
  "tool": "deeptempo-findings_add_finding_to_case",
  "arguments": {
    "case_id": "case-20260121-abc123",
    "finding_id": "f-20260121-def456"
  }
}

These tools are automatically available to Claude when you use the chat interface.
""")


async def main():
    """Main demo entry point."""
    
    print("""
╔═══════════════════════════════════════════════════════════════════════════╗
║                                                                           ║
║             Chat-Driven Case Management Demonstration                     ║
║                                                                           ║
║  This demo shows how Claude can automatically build out security cases    ║
║  based on natural language prompts during investigations.                 ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
""")
    
    print("\nSelect demo mode:")
    print("1. Full Chat Demo (requires Claude API key)")
    print("2. Show Available Tools")
    print("3. Exit")
    
    choice = input("\nEnter choice (1-3): ").strip()
    
    if choice == "1":
        await demo_case_building()
    elif choice == "2":
        demo_direct_mcp()
    elif choice == "3":
        print("Goodbye!")
    else:
        print("Invalid choice")


if __name__ == "__main__":
    asyncio.run(main())

