#!/usr/bin/env python3
"""
Calculate metrics for all existing cases.
This script is useful for populating metrics data for cases that were created
before the metrics system was implemented.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.storage.connection import DatabaseManager
from core.storage.models import Case
from core.cases.case_metrics_service import CaseMetricsService


def main():
    """Calculate metrics for all cases."""
    # Initialize database
    db_manager = DatabaseManager()
    db_manager.initialize()
    
    session = db_manager.get_session()
    
    try:
        # Get all cases
        cases = session.query(Case).all()
        print(f'Found {len(cases)} cases')
        
        if len(cases) == 0:
            print('No cases to process.')
            return
        
        # Calculate metrics for each case
        metrics_service = CaseMetricsService()
        success_count = 0
        
        for case in cases:
            try:
                print(f'Calculating metrics for case {case.case_id} ({case.title})...')
                metrics = metrics_service.calculate_case_metrics(case.case_id, session)
                
                if metrics:
                    success_count += 1
                    print(f'  ✓ Metrics calculated successfully')
                else:
                    print(f'  ✗ Failed to calculate metrics')
            except Exception as e:
                print(f'  ✗ Error: {e}')
        
        session.commit()
        print(f'\n✅ Done! Successfully calculated metrics for {success_count}/{len(cases)} cases')
        
    except Exception as e:
        print(f'Error: {e}')
        session.rollback()
        return 1
    finally:
        session.close()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

