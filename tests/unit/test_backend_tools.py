#!/usr/bin/env python3
"""
Test backend tool integration with Claude function calling.
Tests security-detections tools and other backend tools.
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.claude_service import ClaudeService


def test_security_detections():
    """Test security detection tools"""
    print("\n" + "="*60)
    print("Testing Security Detection Tools")
    print("="*60 + "\n")
    
    # Initialize Claude service with backend tools
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("❌ No API key configured. Please set ANTHROPIC_API_KEY or CLAUDE_API_KEY")
        return False
    
    # Test 1: Analyze coverage for PowerShell techniques
    print("Test 1: Analyze Coverage for PowerShell Techniques")
    print("-" * 60)
    
    try:
        response = claude.chat(
            message="What's our detection coverage for PowerShell-related MITRE techniques T1059.001 and T1059.003?",
            max_tokens=2048
        )
        print(f"✅ Response:\n{response}\n")
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False
    
    # Test 2: Search detections
    print("\nTest 2: Search Detections for 'mimikatz'")
    print("-" * 60)
    
    try:
        response = claude.chat(
            message="Search our detection rules for anything related to 'mimikatz' credential dumping. Show me the top 5 results.",
            max_tokens=2048
        )
        print(f"✅ Response:\n{response}\n")
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False
    
    # Test 3: Get detection counts
    print("\nTest 3: Get Detection Counts by Source")
    print("-" * 60)
    
    try:
        response = claude.chat(
            message="How many detection rules do we have in total? Break it down by source format (Sigma, Splunk, Elastic, KQL).",
            max_tokens=2048
        )
        print(f"✅ Response:\n{response}\n")
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False
    
    print("="*60)
    print("✅ All security detection tool tests passed!")
    print("="*60 + "\n")
    
    return True


def test_backend_integration():
    """Test full backend tool integration"""
    print("\n" + "="*60)
    print("Testing Full Backend Tool Integration")
    print("="*60 + "\n")
    
    # Initialize Claude service with backend tools
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("❌ No API key configured")
        return False
    
    # Test complex query that might use multiple tools
    print("Complex Query: Gap Analysis for Ransomware")
    print("-" * 60)
    
    try:
        response = claude.chat(
            message="""I need to understand our detection gaps for ransomware attacks. 
            Can you analyze our coverage and identify what techniques we're missing detections for?
            Focus on the most critical ransomware techniques.""",
            max_tokens=4096
        )
        print(f"✅ Response:\n{response}\n")
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False
    
    print("="*60)
    print("✅ Backend integration test passed!")
    print("="*60 + "\n")
    
    return True


def test_tool_availability():
    """Test that all tools are loaded correctly"""
    print("\n" + "="*60)
    print("Testing Tool Availability")
    print("="*60 + "\n")
    
    # Initialize Claude service with backend tools
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False
    )
    
    print(f"Backend tools enabled: {claude.use_backend_tools}")
    print(f"Backend tools loaded: {len(claude.backend_tools)}")
    print(f"\nAvailable tools:")
    
    for tool in claude.backend_tools:
        print(f"  - {tool['name']}: {tool['description'][:60]}...")
    
    print(f"\nTotal: {len(claude.backend_tools)} tools")
    print("="*60 + "\n")
    
    return len(claude.backend_tools) > 0


def main():
    """Run all tests"""
    print("\n" + "="*80)
    print(" "*20 + "Backend Tool Integration Tests")
    print("="*80)
    
    # Test 1: Tool availability
    if not test_tool_availability():
        print("\n❌ Tool availability test failed!")
        return 1
    
    # Test 2: Security detection tools
    if not test_security_detections():
        print("\n❌ Security detection tests failed!")
        return 1
    
    # Test 3: Full backend integration
    if not test_backend_integration():
        print("\n❌ Backend integration test failed!")
        return 1
    
    print("\n" + "="*80)
    print(" "*25 + "🎉 ALL TESTS PASSED! 🎉")
    print("="*80 + "\n")
    
    return 0


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)

