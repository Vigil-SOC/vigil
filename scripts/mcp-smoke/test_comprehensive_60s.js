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
  console.log('  COMPREHENSIVE 60-SECOND MCP CONNECTION STATUS TEST');
  console.log('═══════════════════════════════════════════════════════════════\n');

  try {
    const startTime = Date.now();
    
    // Navigate
    console.log('[SETUP] Navigating to http://127.0.0.1:6988...');
    await page.goto('http://127.0.0.1:6988', { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(3000);
    console.log('        ✓ Page loaded\n');
    
    await page.screenshot({ path: 'comprehensive-1-page.png', fullPage: true });
    
    // Open chat
    console.log('[SETUP] Opening CopilotKit chat...');
    const chatButton = await page.$('button[aria-label*="Vigil Chat"]');
    
    if (!chatButton) {
      console.log('        ✗ ERROR: Chat button not found!');
      await browser.close();
      return;
    }
    
    await chatButton.evaluate(el => el.click());
    await page.waitForTimeout(2000);
    console.log('        ✓ Chat opened\n');
    
    await page.screenshot({ path: 'comprehensive-2-chat.png', fullPage: true });
    
    // Dismiss announcement
    const dismissBtn = await page.$('button:has-text("Dismiss")');
    if (dismissBtn) {
      await dismissBtn.click();
      await page.waitForTimeout(500);
    }
    
    // Find input
    const textarea = await page.$('textarea, input[placeholder*="analyst" i]');
    if (!textarea) {
      console.log('[ERROR] Chat input not found!');
      await browser.close();
      return;
    }
    
    // Type and send message
    const message = 'Check MCP connection status';
    console.log('[SEND] Typing message: "' + message + '"');
    await textarea.click();
    await textarea.fill(message);
    await page.waitForTimeout(500);
    
    await page.screenshot({ path: 'comprehensive-3-typed.png', fullPage: true });
    
    console.log('[SEND] Pressing Enter to send...');
    await textarea.press('Enter');
    const sendTime = Date.now();
    await page.waitForTimeout(1000);
    console.log('       ✓ Message sent\n');
    
    console.log('[MONITOR] Watching for response (60 seconds)...\n');
    console.log('Time  | Length | ΔChars | Status Indicators');
    console.log('------|--------|--------|------------------');
    
    let previousContent = '';
    let previousLength = 0;
    let responseLog = [];
    let firstResponseTime = null;
    let finalResponseTime = null;
    
    for (let i = 0; i < 12; i++) {
      await page.waitForTimeout(5000);
      const elapsed = (i + 1) * 5;
      
      // Extract current content
      const chatContent = await page.evaluate(() => {
        const sidebar = document.querySelector('[class*="copilot"]');
        return sidebar ? sidebar.innerText : '';
      });
      
      // Calculate changes
      const currentLength = chatContent.length;
      const deltaChars = currentLength - previousLength;
      const hasNewContent = chatContent !== previousContent;
      
      if (hasNewContent && !firstResponseTime) {
        firstResponseTime = Date.now();
      }
      
      if (hasNewContent) {
        finalResponseTime = Date.now();
      }
      
      // Check for indicators
      const indicators = [];
      if (/calling|executing|using/i.test(chatContent)) indicators.push('CALLING');
      if (/loading|processing|thinking/i.test(chatContent)) indicators.push('LOADING');
      if (/spinning|⏳|⌛|🔄/.test(chatContent)) indicators.push('SPINNER');
      if (/deeptempo|tempo-flow|approval|attack-layer/i.test(chatContent)) indicators.push('SERVERS');
      if (/\d+\s*(connected|total)\s*server/i.test(chatContent)) indicators.push('COUNTS');
      if (/tool|function/i.test(chatContent) && chatContent.includes(message)) indicators.push('TOOL-REF');
      
      const status = indicators.length > 0 ? indicators.join(',') : (hasNewContent ? 'NEW' : 'wait');
      const deltaStr = deltaChars > 0 ? `+${deltaChars}` : (deltaChars < 0 ? `${deltaChars}` : '0');
      
      console.log(`${elapsed}s   | ${currentLength.toString().padStart(6)} | ${deltaStr.padStart(6)} | ${status}`);
      
      await page.screenshot({ path: `comprehensive-4-${elapsed}s.png`, fullPage: true });
      
      responseLog.push({
        time: elapsed,
        length: currentLength,
        delta: deltaChars,
        hasChange: hasNewContent,
        indicators,
        content: chatContent
      });
      
      previousContent = chatContent;
      previousLength = currentLength;
      
      // Check if response is stable
      if (i >= 2 && !hasNewContent && indicators.length === 0) {
        console.log(`\n[INFO] Response appears stable after ${elapsed}s`);
        // Continue monitoring but note stability
      }
    }
    
    const totalElapsed = (Date.now() - sendTime) / 1000;
    
    console.log('\n[EXTRACT] Extracting final response...\n');
    
    const finalContent = await page.evaluate(() => {
      const sidebar = document.querySelector('[class*="copilot"]');
      return sidebar ? sidebar.innerText : '';
    });
    
    fs.writeFileSync('comprehensive-full-content.txt', finalContent);
    
    // Parse AI response
    const parts = finalContent.split(message);
    let aiResponse = 'NO RESPONSE DETECTED';
    
    if (parts.length > 1) {
      aiResponse = parts[1]
        .replace(/Powered by CopilotKit/g, '')
        .replace(/Ask the SOC analyst\.\.\./g, '')
        .trim();
    }
    
    // Analysis
    const hasToolExecution = /calling|executing|using tool|function call/i.test(finalContent);
    const hasServerNames = /deeptempo-findings|tempo-flow|approval|attack-layer/i.test(aiResponse);
    const hasConnectionCounts = /\d+\s*(connected|total|available)\s*server/i.test(aiResponse);
    const isDataRich = aiResponse.length > 200;
    const hasLoadingIndicators = /loading|processing|thinking|\.\.\./.test(finalContent);
    
    const serverMatches = aiResponse.match(/deeptempo-findings|tempo-flow|approval|attack-layer|github|crowdstrike|sentinelone/gi) || [];
    const uniqueServers = [...new Set(serverMatches.map(s => s.toLowerCase()))];
    
    // Report
    console.log('═══════════════════════════════════════════════════════════════');
    console.log('  FINAL REPORT');
    console.log('═══════════════════════════════════════════════════════════════\n');
    
    console.log('1. FULL AI RESPONSE:');
    console.log('   ┌─────────────────────────────────────────────────────────┐');
    if (aiResponse.length > 800) {
      const preview = aiResponse.substring(0, 800);
      preview.split('\n').forEach(line => {
        console.log('   │ ' + line);
      });
      console.log('   │ ... (truncated, full text in comprehensive-full-content.txt)');
    } else {
      aiResponse.split('\n').forEach(line => {
        console.log('   │ ' + line);
      });
    }
    console.log('   └─────────────────────────────────────────────────────────┘');
    console.log(`   Total length: ${aiResponse.length} characters\n`);
    
    console.log('2. DID YOU SEE ACTUAL MCP SERVER DATA?');
    console.log(`   Server names present: ${hasServerNames ? '✓ YES' : '✗ NO'}`);
    if (uniqueServers.length > 0) {
      console.log(`   Servers mentioned: ${uniqueServers.join(', ')}`);
    }
    console.log(`   Connection counts: ${hasConnectionCounts ? '✓ YES' : '✗ NO'}`);
    console.log(`   Data-rich response (>200 chars): ${isDataRich ? '✓ YES' : '✗ NO'}\n`);
    
    console.log('3. HOW LONG DID THE RESPONSE TAKE?');
    if (firstResponseTime) {
      const responseDelay = (firstResponseTime - sendTime) / 1000;
      const streamingDuration = finalResponseTime ? (finalResponseTime - firstResponseTime) / 1000 : 0;
      console.log(`   First response: ${responseDelay.toFixed(1)}s after sending`);
      console.log(`   Streaming duration: ${streamingDuration.toFixed(1)}s`);
      console.log(`   Total time: ${totalElapsed.toFixed(1)}s\n`);
    } else {
      console.log(`   ✗ NO RESPONSE DETECTED in ${totalElapsed.toFixed(1)}s\n`);
    }
    
    console.log('4. ANY TOOL EXECUTION INDICATORS?');
    console.log(`   Tool execution keywords: ${hasToolExecution ? '✓ YES' : '✗ NO'}`);
    console.log(`   Loading indicators: ${hasLoadingIndicators ? '✓ YES' : '✗ NO'}\n`);
    
    console.log('RESPONSE EVOLUTION:');
    console.log('───────────────────');
    responseLog.forEach(log => {
      if (log.hasChange || log.indicators.length > 0) {
        const summary = log.indicators.length > 0 ? log.indicators.join('+') : 'text-only';
        console.log(`   ${log.time}s: ${log.delta > 0 ? '+' : ''}${log.delta} chars [${summary}]`);
      }
    });
    
    console.log('\n═══════════════════════════════════════════════════════════════');
    console.log('  VERDICT');
    console.log('═══════════════════════════════════════════════════════════════\n');
    
    if (hasServerNames && hasConnectionCounts && isDataRich) {
      console.log('   ✅ SUCCESS: Tool executed and returned rich MCP data!');
    } else if (hasServerNames || hasConnectionCounts) {
      console.log('   ⚠️  PARTIAL: Some data present but incomplete');
    } else if (hasToolExecution && aiResponse.length > 100) {
      console.log('   ⚠️  PARTIAL: Tool mentioned but no concrete data');
    } else if (aiResponse.length > 20) {
      console.log('   ❌ FAILED: Only acknowledgment, no tool execution or data');
    } else {
      console.log('   ❌ FAILED: No meaningful response');
    }
    
    console.log('\n   Files saved:');
    console.log('   • comprehensive-*.png (screenshots every 5s)');
    console.log('   • comprehensive-full-content.txt (complete chat content)');
    
    console.log('\n═══════════════════════════════════════════════════════════════\n');
    
    console.log('Keeping browser open for 10 seconds...');
    await page.waitForTimeout(10000);
    
  } catch (error) {
    console.error('\n✗ FATAL ERROR:', error.message);
    await page.screenshot({ path: 'comprehensive-error.png', fullPage: true });
  } finally {
    await browser.close();
  }
})();
