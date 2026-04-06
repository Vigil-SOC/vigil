#!/usr/bin/env python3
"""
Test script to verify SLA assignment functionality.

This script tests:
1. Listing SLA policies
2. Creating a test case
3. Assigning SLA to the case (both auto and manual)
4. Retrieving SLA status
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.case_sla_service import CaseSLAService
from database.connection import get_db_session, init_database
from database.models import SLAPolicy, Case, CaseSLA
import uuid
from datetime import datetime


def test_sla_policies():
    """Test listing SLA policies."""
    print("\n" + "="*60)
    print("TEST 1: List SLA Policies")
    print("="*60)

    init_database()
    session = get_db_session()
    try:
        policies = session.query(SLAPolicy).filter(SLAPolicy.is_active == True).all()
        
        print(f"\nFound {len(policies)} active SLA policies:")
        for policy in policies:
            print(f"\n  Policy ID: {policy.policy_id}")
            print(f"  Name: {policy.name}")
            print(f"  Priority: {policy.priority_level}")
            print(f"  Response Time: {policy.response_time_hours}h")
            print(f"  Resolution Time: {policy.resolution_time_hours}h")
            print(f"  Business Hours Only: {policy.business_hours_only}")
            print(f"  Is Default: {policy.is_default}")
        
        return len(policies) > 0
    finally:
        session.close()


def test_auto_sla_assignment():
    """Test automatic SLA assignment based on priority."""
    print("\n" + "="*60)
    print("TEST 2: Automatic SLA Assignment")
    print("="*60)

    init_database()
    session = get_db_session()
    sla_service = CaseSLAService()
    
    try:
        # Create a test case
        case_id = f"TEST-{uuid.uuid4().hex[:8]}"
        test_case = Case(
            case_id=case_id,
            title="Test Case for SLA Assignment",
            description="This is a test case to verify automatic SLA assignment",
            priority="high",
            status="open",
            severity="medium",
            created_at=datetime.utcnow()
        )
        
        session.add(test_case)
        session.commit()
        print(f"\n✓ Created test case: {case_id}")
        print(f"  Priority: {test_case.priority}")
        
        # Assign SLA automatically (will use default policy for 'high' priority)
        case_sla = sla_service.assign_sla_to_case(case_id, sla_policy_id=None, session=session)
        
        if case_sla:
            print(f"\n✓ SLA automatically assigned!")
            print(f"  SLA ID: {case_sla.sla_id}")
            print(f"  Policy ID: {case_sla.sla_policy_id}")
            print(f"  Response Due: {case_sla.response_due}")
            print(f"  Resolution Due: {case_sla.resolution_due}")
            
            # Get SLA status
            status = sla_service.get_sla_status(case_id, session)
            if status:
                print(f"\n✓ SLA Status Retrieved:")
                print(f"  Health Status: {status['health_status']}")
                print(f"  Response % Elapsed: {status['response_percent_elapsed']:.1f}%")
                print(f"  Resolution % Elapsed: {status['resolution_percent_elapsed']:.1f}%")
                print(f"  Is Breached: {status['is_breached']}")
            
            # Cleanup
            session.delete(case_sla)
            session.delete(test_case)
            session.commit()
            print(f"\n✓ Cleaned up test case")
            
            return True
        else:
            print("\n✗ Failed to assign SLA")
            session.delete(test_case)
            session.commit()
            return False
            
    except Exception as e:
        print(f"\n✗ Error: {e}")
        session.rollback()
        return False
    finally:
        session.close()


def test_manual_sla_assignment():
    """Test manual SLA assignment with specific policy."""
    print("\n" + "="*60)
    print("TEST 3: Manual SLA Assignment")
    print("="*60)

    init_database()
    session = get_db_session()
    sla_service = CaseSLAService()
    
    try:
        # Get a specific policy
        policy = session.query(SLAPolicy).filter(
            SLAPolicy.priority_level == "critical",
            SLAPolicy.is_active == True
        ).first()
        
        if not policy:
            print("\n✗ No critical priority policy found")
            return False
        
        print(f"\n✓ Found policy: {policy.name} ({policy.policy_id})")
        
        # Create a test case with medium priority
        case_id = f"TEST-{uuid.uuid4().hex[:8]}"
        test_case = Case(
            case_id=case_id,
            title="Test Case for Manual SLA Assignment",
            description="This case has medium priority but will get critical SLA",
            priority="medium",
            status="open",
            severity="medium",
            created_at=datetime.utcnow()
        )
        
        session.add(test_case)
        session.commit()
        print(f"\n✓ Created test case: {case_id}")
        print(f"  Priority: {test_case.priority}")
        
        # Manually assign critical SLA policy
        case_sla = sla_service.assign_sla_to_case(
            case_id, 
            sla_policy_id=policy.policy_id,
            session=session
        )
        
        if case_sla:
            print(f"\n✓ SLA manually assigned!")
            print(f"  Policy: {policy.name}")
            print(f"  Response Time: {policy.response_time_hours}h")
            print(f"  Resolution Time: {policy.resolution_time_hours}h")
            print(f"  Response Due: {case_sla.response_due}")
            print(f"  Resolution Due: {case_sla.resolution_due}")
            
            # Cleanup
            session.delete(case_sla)
            session.delete(test_case)
            session.commit()
            print(f"\n✓ Cleaned up test case")
            
            return True
        else:
            print("\n✗ Failed to assign SLA")
            session.delete(test_case)
            session.commit()
            return False
            
    except Exception as e:
        print(f"\n✗ Error: {e}")
        session.rollback()
        return False
    finally:
        session.close()


def test_sla_pause_resume():
    """Test SLA pause and resume functionality."""
    print("\n" + "="*60)
    print("TEST 4: SLA Pause/Resume")
    print("="*60)

    init_database()
    session = get_db_session()
    sla_service = CaseSLAService()
    
    try:
        # Create test case with SLA
        case_id = f"TEST-{uuid.uuid4().hex[:8]}"
        test_case = Case(
            case_id=case_id,
            title="Test Case for SLA Pause/Resume",
            description="Testing pause and resume functionality",
            priority="high",
            status="open",
            severity="medium",
            created_at=datetime.utcnow()
        )
        
        session.add(test_case)
        session.commit()
        print(f"\n✓ Created test case: {case_id}")
        
        # Assign SLA
        case_sla = sla_service.assign_sla_to_case(case_id, session=session)
        if not case_sla:
            print("\n✗ Failed to assign SLA")
            return False
        
        print(f"✓ SLA assigned")
        
        # Pause SLA
        success = sla_service.pause_sla(case_id, reason="Testing pause", session=session)
        if success:
            print(f"✓ SLA paused")
            
            # Check status
            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()
            print(f"  Is Paused: {case_sla.is_paused}")
            print(f"  Paused At: {case_sla.paused_at}")
        else:
            print(f"✗ Failed to pause SLA")
            return False
        
        # Resume SLA
        success = sla_service.resume_sla(case_id, session=session)
        if success:
            print(f"✓ SLA resumed")
            
            # Check status
            case_sla = session.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()
            print(f"  Is Paused: {case_sla.is_paused}")
            print(f"  Total Pause Duration: {case_sla.total_pause_duration}s")
        else:
            print(f"✗ Failed to resume SLA")
            return False
        
        # Cleanup
        session.delete(case_sla)
        session.delete(test_case)
        session.commit()
        print(f"\n✓ Cleaned up test case")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        session.rollback()
        return False
    finally:
        session.close()


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("SLA ASSIGNMENT TEST SUITE")
    print("="*60)
    
    results = {
        "List Policies": test_sla_policies(),
        "Auto Assignment": test_auto_sla_assignment(),
        "Manual Assignment": test_manual_sla_assignment(),
        "Pause/Resume": test_sla_pause_resume(),
    }
    
    print("\n" + "="*60)
    print("TEST RESULTS")
    print("="*60)
    
    for test_name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{test_name:.<40} {status}")
    
    total = len(results)
    passed = sum(results.values())
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

