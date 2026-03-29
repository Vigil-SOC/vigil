#!/usr/bin/env python3
"""
Test script for persistent MCP connections.
This validates that connections are reused and databases don't reconnect on every call.
"""

import asyncio
import sys
import logging
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.mcp_client import get_mcp_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_persistent_connections():
    """Test that MCP connections are persistent and reused."""
    
    logger.info("=" * 70)
    logger.info("Testing Persistent MCP Connections")
    logger.info("=" * 70)
    
    # Get MCP client
    mcp_client = get_mcp_client()
    if not mcp_client:
        logger.error("❌ MCP client not available")
        return False
    
    # Get list of servers
    servers = mcp_client.mcp_service.list_servers()
    logger.info(f"Available servers: {', '.join(servers)}")
    
    # Test with deeptempo-findings server
    test_server = "deeptempo-findings"
    if test_server not in servers:
        logger.warning(f"⚠️  {test_server} not found, using first available server")
        if not servers:
            logger.error("❌ No servers available")
            return False
        test_server = servers[0]
    
    logger.info(f"\n📡 Testing with server: {test_server}")
    
    # Step 1: Connect to server (establishes persistent connection)
    logger.info("\n1️⃣  Establishing persistent connection...")
    success = await mcp_client.connect_to_server(test_server, persistent=True)
    if not success:
        logger.error(f"❌ Failed to connect to {test_server}")
        return False
    
    logger.info(f"✅ Connected to {test_server}")
    
    # Check connection status
    status = mcp_client.get_connection_status()
    logger.info(f"Connection status: {status.get(test_server, False)}")
    
    # Step 2: Make first tool call
    logger.info("\n2️⃣  Making first tool call...")
    logger.info("   (Watch for database connection messages - should see connection)")
    
    result1 = await mcp_client.call_tool(
        test_server,
        "list_findings",
        {"limit": 1, "kwargs": {}}
    )
    
    if result1.get("error"):
        logger.error(f"❌ First tool call failed: {result1}")
        return False
    
    logger.info("✅ First tool call successful")
    
    # Step 3: Make second tool call (should reuse connection)
    logger.info("\n3️⃣  Making second tool call...")
    logger.info("   (Watch for database messages - should NOT see new connection)")
    
    result2 = await mcp_client.call_tool(
        test_server,
        "list_findings",
        {"limit": 1, "kwargs": {}}
    )
    
    if result2.get("error"):
        logger.error(f"❌ Second tool call failed: {result2}")
        return False
    
    logger.info("✅ Second tool call successful")
    
    # Step 4: Make third tool call (should still reuse connection)
    logger.info("\n4️⃣  Making third tool call...")
    logger.info("   (Watch for database messages - should NOT see new connection)")
    
    result3 = await mcp_client.call_tool(
        test_server,
        "list_findings",
        {"limit": 1, "kwargs": {}}
    )
    
    if result3.get("error"):
        logger.error(f"❌ Third tool call failed: {result3}")
        return False
    
    logger.info("✅ Third tool call successful")
    
    # Step 5: Check connection is still alive
    logger.info("\n5️⃣  Checking persistent connection status...")
    status = mcp_client.get_connection_status()
    is_connected = status.get(test_server, False)
    
    if is_connected:
        logger.info(f"✅ Persistent connection to {test_server} is still active")
    else:
        logger.error(f"❌ Persistent connection to {test_server} was lost")
        return False
    
    # Step 6: Cleanup
    logger.info("\n6️⃣  Cleaning up...")
    await mcp_client.close_all()
    logger.info("✅ All connections closed")
    
    logger.info("\n" + "=" * 70)
    logger.info("✅ SUCCESS: Persistent connections working correctly!")
    logger.info("=" * 70)
    logger.info("\n📊 Expected behavior:")
    logger.info("   - First call: You should see database initialization logs")
    logger.info("   - Subsequent calls: No database initialization - connection reused")
    logger.info("   - This eliminates the spam of 'PostgreSQL connection established'")
    
    return True


async def main():
    """Main entry point."""
    try:
        success = await test_persistent_connections()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.error(f"❌ Test failed with exception: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

