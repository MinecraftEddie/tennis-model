#!/usr/bin/env python
"""
Unified test runner for evaluator module.
Runs all test suites: rules, momentum, risk_flags.
"""

import sys
import os

# Add parent directory to path so we can import tennis_model
project_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(project_dir))  # Parent of tennis_model

def run_all_tests():
    """Run all evaluator test suites."""
    
    print("\n" + "="*100)
    print(" "*30 + "EVALUATOR MODULE TEST SUITE")
    print("="*100)
    
    # Test 1: Rules
    try:
        print("\n[1/3] Importing and running RULES tests...")
        from tennis_model.evaluator import rules
        rules.test_rules()
        print("✓ RULES tests passed")
    except Exception as e:
        print(f"✗ RULES tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 2: Momentum
    try:
        print("\n[2/3] Importing and running MOMENTUM tests...")
        from tennis_model.evaluator import momentum
        momentum.test_momentum()
        print("✓ MOMENTUM tests passed")
    except Exception as e:
        print(f"✗ MOMENTUM tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 3: Risk Flags
    try:
        print("\n[3/3] Importing and running RISK_FLAGS tests...")
        from tennis_model.evaluator import risk_flags
        risk_flags.test_risk_flags()
        print("✓ RISK_FLAGS tests passed")
    except Exception as e:
        print(f"✗ RISK_FLAGS tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    print("\n" + "="*100)
    print(" "*35 + "ALL TESTS PASSED ✓")
    print("="*100 + "\n")
    return True


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
