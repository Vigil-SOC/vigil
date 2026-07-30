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

  console.log('═══════════════════════════════════════════════════════');
  console.log('  COPILOTKIT MCP TOOLS TEST - DETAILED MONITORING');
  console.log('═══════════════════════════════════════════════════════\n');

  try {
    // Step 1: Navigate
    console.log('[1/7] Navigating to http://127.0.0.1:6988...');
    await page.goto('http://127.0.0.1:6988', { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(3000);
    console.log('      ✓ Page loaded\n');
    
    await page.screenshot({ path: 'final-test-1-loaded.png', fullPage: true });
    
    // Step 2: Find and open chat
    console.log('[2/7] Looking for CopilotKit chat...');
    const chatButton = await page.$('button[aria-label*="chat" i], button[aria-label*="copilot" i]');
    
    if (!chatButton) {
      console.log('      ✗ Chat button not found!\n');
      await page.screenshot({ path: 'final-test-error.png', fullPage: true });
      await browser.close();
      return;
    }
    
    const buttonLabel = await chatButton.getAttribute('aria-label');
    console.log(`      ✓ Found: "${buttonLabel}"`);
    console.log('      → Opening chat...');
    
    await chatButton.evaluate(el => el.click());
    await page.waitForTimeout(2000);
    console.log('      ✓ Chat opened\n');
    
    await page.screenshot({ path: 'final-test-2-chat-open.png', fullPage: true });
    
    // Dismiss announcement
    const dismissBtn = await page.$('button:has-text("Dismiss")');
    if (dismissBtn) {
      await dismissBtn.click();
      await page.waitForTimeout(500);
    }
    
    // Step 3: Find input
    console.log('[3/7] Locating chat input...');
    const textarea = await page.$('textarea, input[placeholder*="analyst" i]');
    
    if (!textarea) {
      console.log('      ✗ Chat input not found!\n');
      await browser.close();
      return;
    }
    
    console.log('      ✓ Input field located\n');
    
    // Step 4: Type message
    const message = 'What MCP tools are available? Use the list_mcp_tools tool.';
    console.log('[4/7] Typing message...');
    console.log(`      "${message}"`);
    
    await textarea.click();
    await textarea.fill(message);
    await page.waitForTimeout(500);
    console.log('      ✓ Message typed\n');
    
    await page.screenshot({ path: 'final-test-3-message-ready.png', fullPage: true });
    
    // Step 5: Send
    console.log('[5/7] Sending message...');
    await textarea.press('Enter');
    await page.waitForTimeout(1000);
    console.log('      ✓ Message sent\n');
    
    // Step 6: Monitor response
    console.log('[6/7] Monitoring response (60 seconds)...\n');
    
    let previousContent = '';
    let responseComplete = false;
    
    for (let elapsed = 0; elapsed <= 60; elapsed += 5) {
      await page.waitForTimeout(5000);
      
      // Capture current state
      const chatContent = await page.evaluate(() => {
        const sidebar = document.querySelector('[class*="copilot"]');
        return sidebar ? sidebar.innerText : '';
      });
      
      const hasNewContent = chatContent !== previousContent;
      const hasToolIndicator = /calling|executing|loading|thinking/i.test(chatContent);
      const hasResponse = chatContent.includes(message) && chatContent.length > message.length + 100;
      
      // Status indicator
      let status = '⏳';
      if (hasNewContent) status = '📝';
      if (hasToolIndicator) status = '🔧';
      if (hasResponse && !hasNewContent) status = '✓';
      
      console.log(`      ${status} ${elapsed + 5}s | Content: ${chatContent.length} chars | New: ${hasNewContent ? 'YES' : 'NO'} | Tool: ${hasToolIndicator ? 'YES' : 'NO'}`);
      
      await page.screenshot({ path: `final-test-4-response-${elapsed + 5}s.png`, fullPage: true });
      
      if (hasResponse && !hasNewContent && elapsed > 10) {
        console.log('      → Response appears stable');
        responseComplete = true;
      }
      
      previousContent = chatContent;
      
      if (responseComplete) {
        console.log('      ✓ Response complete\n');
        break;
      }
    }
    
    // Step 7: Extract and analyze
    console.log('[7/7] Extracting response...\n');
    
    const finalContent = await page.evaluate(() => {
      const sidebar = document.querySelector('[class*="copilot"]');
      return sidebar ? sidebar.innerText : '';
    });
    
    fs.writeFileSync('final-test-chat-content.txt', finalContent);
    
    // Parse out the AI response
    const parts = finalContent.split(message);
    let aiResponse = 'NO RESPONSE DETECTED';
    
    if (parts.length > 1) {
      aiResponse = parts[1]
        .replace(/Powered by CopilotKit/g, '')
        .replace(/Ask the SOC analyst\.\.\./g, '')
        .trim();
    }
    
    // Check for indicators
    const hasError = /error|failed|cannot|don't have|no access/i.test(aiResponse);
    const hasToolCall = /calling|executing|tool|function/i.test(finalContent);
    const hasLoadingState = /loading|thinking|processing/i.test(finalContent);
    
    const toolNames = [
      'list_mcp_tools',
      'search_findings',
      'get_finding_details',
      'call_mcp_tool',
      'get_mcp_connection_status',
      'deeptempo-findings',
      'tempo-flow',
      'approval',
      'attack-layer'
    ];
    
    const mentionedTools = toolNames.filter(tool => 
      aiResponse.toLowerCase().includes(tool.toLowerCase()) ||
      finalContent.toLowerCase().includes(tool.toLowerCase())
    );
    
    // Report
    console.log('═══════════════════════════════════════════════════════');
    console.log('  RESULTS');
    console.log('═══════════════════════════════════════════════════════\n');
    
    console.log('1. DID THE CHAT OPEN?');
    console.log('   ✓ YES\n');
    
    console.log('2. FULL RESPONSE TEXT FROM AI:');
    console.log('   ┌─────────────────────────────────────────────────┐');
    if (aiResponse.length > 500) {
      console.log('   │ ' + aiResponse.substring(0, 500).split('\n').join('\n   │ '));
      console.log('   │ ... (truncated, see final-test-chat-content.txt)');
    } else {
      aiResponse.split('\n').forEach(line => {
        console.log('   │ ' + line);
      });
    }
    console.log('   └─────────────────────────────────────────────────┘\n');
    
    console.log('3. ERROR INDICATORS?');
    console.log(`   ${hasError ? '✗ YES - Errors detected' : '✓ NO - No errors'}\n`);
    
    console.log('4. TOOL CALLING INDICATORS OR LOADING STATES?');
    console.log(`   Tool calling: ${hasToolCall ? '✓ YES' : '✗ NO'}`);
    console.log(`   Loading state: ${hasLoadingState ? '✓ YES' : '✗ NO'}\n`);
    
    console.log('ADDITIONAL ANALYSIS:');
    console.log(`   Response length: ${aiResponse.length} characters`);
    console.log(`   Tools mentioned: ${mentionedTools.length > 0 ? mentionedTools.join(', ') : 'NONE'}`);
    
    console.log('\n═══════════════════════════════════════════════════════');
    console.log('  FINAL VERDICT');
    console.log('═══════════════════════════════════════════════════════\n');
    
    if (mentionedTools.length > 0 && !hasError) {
      console.log('   ✅ SUCCESS: MCP tools were listed!');
    } else if (hasError) {
      console.log('   ❌ FAILED: Error accessing MCP tools');
    } else if (aiResponse.length > 50 && mentionedTools.length === 0) {
      console.log('   ⚠️  PARTIAL: Response received but no tool names');
    } else {
      console.log('   ❌ FAILED: No MCP tool data returned');
    }
    
    console.log('\n   Files saved:');
    console.log('   • final-test-*.png (screenshots at each stage)');
    console.log('   • final-test-chat-content.txt (full chat content)');
    
    console.log('\n═══════════════════════════════════════════════════════\n');
    
    console.log('Waiting 10 seconds before closing browser...');
    await page.waitForTimeout(10000);
    
  } catch (error) {
    console.error('\n❌ FATAL ERROR:', error.message);
    console.error(error.stack);
    await page.screenshot({ path: 'final-test-error.png', fullPage: true });
  } finally {
    await browser.close();
  }
})();
