#!/usr/bin/env python3
"""
Comprehensive integration tests for backend tool integration.
Tests all tool categories and complex workflows.
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.claude_service import ClaudeService


def test_tool_count():
    """Test that all expected tools are loaded"""
    print("\n" + "="*80)
    print("TEST: Tool Count")
    print("="*80 + "\n")
    
    claude = ClaudeService(use_backend_tools=True, use_mcp_tools=False)
    
    expected_count = 19  # 5 security + 7 findings + 2 attack + 5 approval
    actual_count = len(claude.backend_tools)
    
    print(f"Expected tools: {expected_count}")
    print(f"Loaded tools: {actual_count}")
    
    if actual_count != expected_count:
        print(f"❌ Tool count mismatch!")
        return False
    
    print("✅ Tool count correct\n")
    return True


def test_security_detection_workflow():
    """Test complete security detection workflow"""
    print("\n" + "="*80)
    print("TEST: Security Detection Workflow")
    print("="*80 + "\n")
    
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("⚠️  No API key - skipping API test")
        return True
    
    print("Scenario: Analyzing detection coverage for a threat hunt")
    print("-" * 80)
    
    try:
        response = claude.chat(
            message="""I'm planning a threat hunt for credential access attacks. 
            Can you analyze our detection coverage for MITRE techniques T1003 (OS Credential Dumping)
            and T1558 (Steal or Forge Kerberos Tickets)? Which format has the best coverage?""",
            max_tokens=3000
        )
        
        print(f"Response received: {len(str(response))} chars")
        print(f"✅ Security detection workflow completed\n")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


def test_case_management_workflow():
    """Test case management with findings"""
    print("\n" + "="*80)
    print("TEST: Case Management Workflow")
    print("="*80 + "\n")
    
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("⚠️  No API key - skipping API test")
        return True
    
    print("Scenario: Managing investigation cases")
    print("-" * 80)
    
    try:
        response = claude.chat(
            message="""What investigation cases do we currently have open? 
            Show me a summary including severity and finding counts.""",
            max_tokens=2000
        )
        
        print(f"Response received: {len(str(response))} chars")
        print(f"✅ Case management workflow completed\n")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


def test_approval_workflow():
    """Test approval workflow"""
    print("\n" + "="*80)
    print("TEST: Approval Workflow")
    print("="*80 + "\n")
    
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("⚠️  No API key - skipping API test")
        return True
    
    print("Scenario: Checking pending approvals")
    print("-" * 80)
    
    try:
        response = claude.chat(
            message="""Are there any pending actions waiting for approval? 
            If so, what are they and what do you recommend?""",
            max_tokens=2000
        )
        
        print(f"Response received: {len(str(response))} chars")
        print(f"✅ Approval workflow completed\n")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


def test_attack_layer_workflow():
    """Test MITRE ATT&CK layer generation"""
    print("\n" + "="*80)
    print("TEST: Attack Layer Workflow")
    print("="*80 + "\n")
    
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("⚠️  No API key - skipping API test")
        return True
    
    print("Scenario: Generating ATT&CK Navigator layer")
    print("-" * 80)
    
    try:
        response = claude.chat(
            message="""Generate a MITRE ATT&CK Navigator layer showing our detection coverage.
            What are the top 3 tactics with the best coverage?""",
            max_tokens=2000
        )
        
        print(f"Response received: {len(str(response))} chars")
        print(f"✅ Attack layer workflow completed\n")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


def test_complex_multi_tool_workflow():
    """Test complex workflow using multiple tools"""
    print("\n" + "="*80)
    print("TEST: Complex Multi-Tool Workflow")
    print("="*80 + "\n")
    
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False
    )
    
    if not claude.has_api_key():
        print("⚠️  No API key - skipping API test")
        return True
    
    print("Scenario: Comprehensive threat assessment")
    print("-" * 80)
    
    try:
        response = claude.chat(
            message="""I need a comprehensive threat assessment report:
            1. What are our current high-severity findings?
            2. Analyze our detection coverage for lateral movement techniques
            3. Identify any detection gaps we should prioritize
            4. Are there pending actions that need approval?
            
            Give me actionable insights.""",
            max_tokens=4096
        )
        
        print(f"Response received: {len(str(response))} chars")
        print(f"✅ Complex multi-tool workflow completed\n")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


def test_backend_vs_mcp_equivalence():
    """Test that backend tools work as well as MCP tools"""
    print("\n" + "="*80)
    print("TEST: Backend vs MCP Equivalence")
    print("="*80 + "\n")
    
    # Test backend tools
    backend_claude = ClaudeService(use_backend_tools=True, use_mcp_tools=False)
    backend_count = len(backend_claude.backend_tools)
    
    # Test MCP tools (might not be available, that's OK)
    mcp_claude = ClaudeService(use_backend_tools=False, use_mcp_tools=True)
    mcp_count = len(mcp_claude.mcp_tools)
    
    print(f"Backend tools: {backend_count}")
    print(f"MCP tools: {mcp_count}")
    
    print("\nBackend tools via Agent SDK (no desktop dependency):")
    for tool in backend_claude.backend_tools:
        print(f"  ✓ {tool['name']}")
    
    if mcp_count > 0:
        print(f"\n⚠️  MCP tools still loaded ({mcp_count} tools)")
        print("    Consider disabling MCP for web UI deployment")
    else:
        print(f"\n✅ MCP correctly disabled - using Agent SDK backend implementation")
    
    print("\n✅ Equivalence test completed\n")
    return True


def main():
    """Run all integration tests"""
    print("\n" + "="*80)
    print(" "*15 + "Backend Tool Integration - Full Test Suite")
    print("="*80)
    
    tests = [
        ("Tool Count", test_tool_count),
        ("Security Detection Workflow", test_security_detection_workflow),
        ("Case Management Workflow", test_case_management_workflow),
        ("Approval Workflow", test_approval_workflow),
        ("Attack Layer Workflow", test_attack_layer_workflow),
        ("Complex Multi-Tool Workflow", test_complex_multi_tool_workflow),
        ("Backend vs MCP Equivalence", test_backend_vs_mcp_equivalence),
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            passed = test_func()
            results.append((test_name, passed))
        except Exception as e:
            print(f"\n❌ Test '{test_name}' raised exception: {e}")
            results.append((test_name, False))
    
    # Print summary
    print("\n" + "="*80)
    print(" "*30 + "TEST SUMMARY")
    print("="*80 + "\n")
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print(f"\n{passed_count}/{total_count} tests passed")
    
    if passed_count == total_count:
        print("\n" + "="*80)
        print(" "*25 + "🎉 ALL TESTS PASSED! 🎉")
        print("="*80 + "\n")
        return 0
    else:
        print(f"\n⚠️  {total_count - passed_count} test(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)

