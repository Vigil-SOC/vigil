const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({ 
    headless: false,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 }
  });
  const page = await context.newPage();

  console.log('═══════════════════════════════════════════════════════════════');
  console.log('  TESTING: get_mcp_connection_status tool');
  console.log('═══════════════════════════════════════════════════════════════\n');

  try {
    // Navigate
    console.log('→ Navigating to http://127.0.0.1:6988...');
    await page.goto('http://127.0.0.1:6988', { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(3000);
    console.log('✓ Page loaded\n');
    
    await page.screenshot({ path: 'get-mcp-status-1-page.png', fullPage: true });
    
    // Find and open chat
    console.log('→ Looking for CopilotKit chat button...');
    const chatButton = await page.$('button[aria-label*="Vigil Chat"]');
    
    if (!chatButton) {
      console.log('✗ ERROR: Chat button not found!');
      await browser.close();
      return;
    }
    
    console.log('✓ Found chat button');
    await chatButton.evaluate(el => el.click());
    await page.waitForTimeout(2000);
    console.log('✓ Chat opened\n');
    
    await page.screenshot({ path: 'get-mcp-status-2-chat.png', fullPage: true });
    
    // Dismiss announcement if present
    const dismissBtn = await page.$('button:has-text("Dismiss")');
    if (dismissBtn) {
      await dismissBtn.click();
      await page.waitForTimeout(500);
    }
    
    // Find input
    console.log('→ Finding chat input...');
    const textarea = await page.$('textarea, input[placeholder*="analyst" i]');
    
    if (!textarea) {
      console.log('✗ ERROR: Chat input not found!');
      await browser.close();
      return;
    }
    
    console.log('✓ Chat input located\n');
    
    // Type exact message
    const message = 'Use the get_mcp_connection_status tool to check which MCP servers are connected';
    console.log('→ Typing message:');
    console.log(`  "${message}"\n`);
    
    await textarea.click();
    await textarea.fill(message);
    await page.waitForTimeout(500);
    
    await page.screenshot({ path: 'get-mcp-status-3-typed.png', fullPage: true });
    
    // Send
    console.log('→ Sending message...');
    await textarea.press('Enter');
    await page.waitForTimeout(1000);
    console.log('✓ Message sent\n');
    
    console.log('→ Monitoring for 30 seconds (checking every 3 seconds)...\n');
    
    let previousContent = '';
    const snapshots = [];
    
    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(3000);
      const elapsed = (i + 1) * 3;
      
      // Get current content
      const chatContent = await page.evaluate(() => {
        const sidebar = document.querySelector('[class*="copilot"]');
        return sidebar ? sidebar.innerText : '';
      });
      
      const fullPageContent = await page.evaluate(() => document.body.innerText);
      
      // Check for various indicators
      const hasNewContent = chatContent !== previousContent;
      const hasThinking = /thinking|processing|loading|\.\.\./.test(chatContent);
      const hasToolCall = /calling|executing|using tool|get_mcp_connection_status/i.test(chatContent);
      const hasServerData = /deeptempo|tempo-flow|approval|attack-layer|connected|servers/i.test(chatContent);
      
      // Visual indicator
      let icon = '⏳';
      if (hasToolCall) icon = '🔧';
      else if (hasServerData) icon = '📊';
      else if (hasNewContent) icon = '📝';
      else if (hasThinking) icon = '💭';
      
      const status = `${icon} ${elapsed}s | ${chatContent.length} chars | New:${hasNewContent?'Y':'N'} Tool:${hasToolCall?'Y':'N'} Data:${hasServerData?'Y':'N'} Think:${hasThinking?'Y':'N'}`;
      console.log(`  ${status}`);
      
      snapshots.push({
        time: elapsed,
        length: chatContent.length,
        hasToolCall,
        hasServerData,
        hasThinking,
        content: chatContent
      });
      
      await page.screenshot({ path: `get-mcp-status-4-${elapsed}s.png`, fullPage: true });
      
      previousContent = chatContent;
    }
    
    console.log('\n→ Extracting final response...\n');
    
    const finalContent = await page.evaluate(() => {
      const sidebar = document.querySelector('[class*="copilot"]');
      return sidebar ? sidebar.innerText : '';
    });
    
    fs.writeFileSync('get-mcp-status-full-content.txt', finalContent);
    
    // Extract AI response
    const parts = finalContent.split(message);
    let aiResponse = 'NO RESPONSE DETECTED';
    
    if (parts.length > 1) {
      aiResponse = parts[1]
        .replace(/Powered by CopilotKit/g, '')
        .replace(/Ask the SOC analyst\.\.\./g, '')
        .trim();
    }
    
    // Analysis
    const hasToolIndicator = /calling|executing|using.*tool|get_mcp_connection_status/i.test(aiResponse);
    const hasThinkingIndicator = /thinking|processing|analyzing|checking/i.test(aiResponse);
    const hasLoadingIndicator = /loading|\.\.\./.test(aiResponse);
    
    const serverNames = ['deeptempo-findings', 'tempo-flow', 'approval', 'attack-layer'];
    const mentionedServers = serverNames.filter(name => aiResponse.toLowerCase().includes(name));
    
    const hasConnectionData = /connected|total.*server|(\d+)\s+server/i.test(aiResponse);
    
    // Report
    console.log('═══════════════════════════════════════════════════════════════');
    console.log('  REPORT');
    console.log('═══════════════════════════════════════════════════════════════\n');
    
    console.log('1. DID YOU SEND THE MESSAGE?');
    console.log('   ✓ YES - Message sent successfully\n');
    
    console.log('2. WHAT WAS THE FULL AI RESPONSE?');
    console.log('   ┌───────────────────────────────────────────────────────────┐');
    if (aiResponse === 'NO RESPONSE DETECTED') {
      console.log('   │ ✗ NO RESPONSE DETECTED');
    } else if (aiResponse.length > 400) {
      const lines = aiResponse.substring(0, 400).split('\n');
      lines.forEach(line => console.log('   │ ' + line));
      console.log('   │ ... (truncated)');
    } else {
      const lines = aiResponse.split('\n');
      lines.forEach(line => console.log('   │ ' + line));
    }
    console.log('   └───────────────────────────────────────────────────────────┘');
    console.log(`   Length: ${aiResponse.length} characters\n`);
    
    console.log('3. ANY LOADING/THINKING INDICATORS?');
    console.log(`   Thinking/Processing: ${hasThinkingIndicator ? '✓ YES' : '✗ NO'}`);
    console.log(`   Loading: ${hasLoadingIndicator ? '✓ YES' : '✗ NO'}\n`);
    
    console.log('4. ANY TOOL EXECUTION INDICATORS?');
    console.log(`   Tool call mention: ${hasToolIndicator ? '✓ YES' : '✗ NO'}`);
    console.log(`   Tool name in response: ${aiResponse.includes('get_mcp_connection_status') ? '✓ YES' : '✗ NO'}\n`);
    
    console.log('ADDITIONAL ANALYSIS:');
    console.log(`   Server names mentioned: ${mentionedServers.length > 0 ? mentionedServers.join(', ') : 'NONE'}`);
    console.log(`   Connection data present: ${hasConnectionData ? '✓ YES' : '✗ NO'}`);
    
    // Timeline summary
    console.log('\n   Response Timeline:');
    snapshots.forEach(snap => {
      const indicators = [];
      if (snap.hasToolCall) indicators.push('TOOL');
      if (snap.hasServerData) indicators.push('DATA');
      if (snap.hasThinking) indicators.push('THINK');
      const status = indicators.length > 0 ? indicators.join('+') : 'waiting';
      console.log(`     ${snap.time}s: ${snap.length} chars [${status}]`);
    });
    
    console.log('\n═══════════════════════════════════════════════════════════════');
    console.log('  VERDICT');
    console.log('═══════════════════════════════════════════════════════════════\n');
    
    if (mentionedServers.length > 0 && hasConnectionData) {
      console.log('   ✅ SUCCESS: Tool returned MCP server connection data!');
    } else if (hasToolIndicator && aiResponse.length > 100) {
      console.log('   ⚠️  PARTIAL: Tool called but incomplete/no data');
    } else if (aiResponse.length > 20) {
      console.log('   ❌ FAILED: Response received but no tool execution or data');
    } else {
      console.log('   ❌ FAILED: No meaningful response');
    }
    
    console.log('\n   Files saved:');
    console.log('   • get-mcp-status-*.png');
    console.log('   • get-mcp-status-full-content.txt');
    
    console.log('\n═══════════════════════════════════════════════════════════════\n');
    
    console.log('Waiting 10 seconds before closing...');
    await page.waitForTimeout(10000);
    
  } catch (error) {
    console.error('\n✗ ERROR:', error.message);
    await page.screenshot({ path: 'get-mcp-status-error.png', fullPage: true });
  } finally {
    await browser.close();
  }
})();
