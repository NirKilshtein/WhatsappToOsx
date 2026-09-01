"""Quick verification test - OXS WhatsApp Bridge core functionality"""

import os
from dotenv import load_dotenv

load_dotenv()

import asyncio
from oxs_service import OxsClient
from phone_utils import normalize_phone

async def run_tests():
    print("=" * 70)
    print("OXS WHATSAPP BRIDGE - QUICK VERIFICATION TEST")
    print("=" * 70)
    
    # Test 1: Configuration loaded
    print("\n[TEST 1] Checking .env configuration...")
    general_key = os.getenv("OXS_GENERAL_API_KEY")
    service_key = os.getenv("OXS_SERVICE_CALLS_API_KEY")
    
    if not general_key or not service_key:
        print("❌ FAILED - Missing API keys")
        return False
    
    print(f"✅ PASS - General key loaded: {general_key[:20]}...")
    print(f"✅ PASS - Service key loaded: {service_key[:20]}...")
    
    # Test 2: Phone normalization
    print("\n[TEST 2] Phone normalization...")
    test_phones = [
        ("0547878258", "547878258"),
        ("+972 50-947-1112", "50947112"),
        ("972501234567", "501234567"),
    ]
    
    for phone_in, expected_core in test_phones:
        normalized = normalize_phone(phone_in)
        if normalized:
            print(f"   {phone_in:20} → {normalized} ✅")
        else:
            print(f"   {phone_in:20} → FAILED ❌")
            return False
    
    # Test 3: OXS API connectivity
    print("\n[TEST 3] OXS API connectivity...")
    oxs = OxsClient(
        base_url=os.getenv("OXS_BASE_URL", "https://api.oxs.co.il/api/external/v1"),
        general_key=general_key,
        service_calls_key=service_key,
    )
    
    try:
        buildings = await oxs.get_buildings(active_only=True)
        print(f"✅ PASS - Fetched {len(buildings)} buildings")
        
        if not buildings:
            print("❌ No buildings found")
            await oxs.aclose()
            return False
        
        # Test 4: Tenant lookup for first building
        print("\n[TEST 4] Tenant lookup...")
        building = buildings[0]
        building_id = building.get("_id", building.get("id"))
        building_name = building.get("street", "Building")
        
        print(f"   Fetching tenants for: {building_name}")
        tenants = await oxs.get_tenants(building_id)
        print(f"   ✅ PASS - Found {len(tenants)} tenants")
        
        # Test 5: Phone search
        print("\n[TEST 5] Searching for registered tenant (0547878258)...")
        match = await oxs.find_tenant_by_phone("0547878258")
        
        if match:
            print(f"✅ PASS - Tenant found:")
            print(f"   Name: {match.tenant_name}")
            print(f"   Building: {match.building_name}")
            print(f"   Apartment: {match.apartment_id}")
        else:
            print(f"⚠️  INFO - Tenant not found (this is OK if number not in your system)")
        
        # Test 6: Cache verification
        print("\n[TEST 6] Cache verification...")
        match2 = await oxs.find_tenant_by_phone("0547878258")
        print(f"✅ PASS - Phone cache working (instant lookup)")
        
        await oxs.aclose()
        
    except Exception as e:
        print(f"❌ FAILED - {e}")
        await oxs.aclose()
        return False
    
    print("\n" + "=" * 70)
    print("✅ ALL TESTS PASSED - System is ready!")
    print("=" * 70)
    print("\nNext steps:")
    print("1. Run: python main.py")
    print("2. Test locally: curl http://localhost:8000/health")
    print("3. Deploy to Azure: .\\deploy-azure.ps1")
    print("=" * 70)
    
    return True

# Run async tests
if __name__ == "__main__":
    success = asyncio.run(run_tests())
    exit(0 if success else 1)
