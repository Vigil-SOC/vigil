/**
 * Frontend logging service with structured logging similar to backend.
 * 
 * Log levels:
 * - DEBUG: Detailed diagnostic info (filtered out in production)
 * - INFO: General informational messages
 * - WARN: Warning messages for unexpected but handled situations
 * - ERROR: Error messages for failures
 */

import { basePath } from '../config/basePath'

enum LogLevel {
  DEBUG = 0,
  INFO = 1,
  WARN = 2,
  ERROR = 3,
}

class Logger {
  private minLevel: LogLevel
  private component: string
  private remoteLoggingEnabled: boolean = true

  constructor(component: string, minLevel: LogLevel = LogLevel.DEBUG) {
    this.component = component
    // In production, only show INFO and above
    this.minLevel = import.meta.env.PROD ? LogLevel.INFO : minLevel
  }

  private async sendToBackend(level: string, message: string, ...args: any[]) {
    // Only send INFO+ to backend to avoid spam
    if (!this.remoteLoggingEnabled || level === 'DEBUG') return

    try {
      // Extract structured data from args
      const extra: Record<string, any> = {}
      args.forEach((arg, index) => {
        if (typeof arg === 'object' && arg !== null) {
          Object.assign(extra, arg)
        } else {
          extra[`arg${index}`] = arg
        }
      })

      // Fire and forget - don't await to avoid blocking
      fetch(`${basePath}/api/logs/frontend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          level,
          message,
          component: this.component,
          timestamp: new Date().toISOString(),
          extra: Object.keys(extra).length > 0 ? extra : undefined
        })
      }).catch(() => {
        // Silently fail - don't break app if logging fails
      })
    } catch {
      // Silently fail - don't break app if logging fails
    }
  }

  private log(level: LogLevel, emoji: string, message: string, ...args: any[]) {
    if (level < this.minLevel) return

    const prefix = `${emoji} [${this.component}]`
    const fullMessage = `${prefix} ${message}`

    // Convert level enum to string
    const levelStr = LogLevel[level]

    switch (level) {
      case LogLevel.DEBUG:
        console.debug(fullMessage, ...args)
        break
      case LogLevel.INFO:
        console.log(fullMessage, ...args)
        break
      case LogLevel.WARN:
        console.warn(fullMessage, ...args)
        break
      case LogLevel.ERROR:
        console.error(fullMessage, ...args)
        break
    }

    // Send to backend (async, fire-and-forget)
    this.sendToBackend(levelStr, message, ...args)
  }

  debug(message: string, ...args: any[]) {
    this.log(LogLevel.DEBUG, '🔍', message, ...args)
  }

  info(message: string, ...args: any[]) {
    this.log(LogLevel.INFO, 'ℹ️', message, ...args)
  }

  warn(message: string, ...args: any[]) {
    this.log(LogLevel.WARN, '⚠️', message, ...args)
  }

  error(message: string, ...args: any[]) {
    this.log(LogLevel.ERROR, '❌', message, ...args)
  }

  // Specialized log methods with custom emojis
  send(message: string, ...args: any[]) {
    this.log(LogLevel.INFO, '🚀', message, ...args)
  }

  receive(message: string, ...args: any[]) {
    this.log(LogLevel.INFO, '📥', message, ...args)
  }

  request(message: string, ...args: any[]) {
    this.log(LogLevel.DEBUG, '📤', message, ...args)
  }

  render(message: string, ...args: any[]) {
    this.log(LogLevel.DEBUG, '🎨', message, ...args)
  }

  block(message: string, ...args: any[]) {
    this.log(LogLevel.DEBUG, '📦', message, ...args)
  }

  thinking(message: string, ...args: any[]) {
    this.log(LogLevel.INFO, '💭', message, ...args)
  }

  success(message: string, ...args: any[]) {
    this.log(LogLevel.INFO, '✅', message, ...args)
  }

  investigate(message: string, ...args: any[]) {
    this.log(LogLevel.INFO, '🔍', message, ...args)
  }
}

/**
 * Create a logger instance for a specific component.
 * 
 * @param component - Component name (e.g., 'ClaudeDrawer', 'ChatWindow')
 * @returns Logger instance
 */
export function createLogger(component: string): Logger {
  return new Logger(component)
}

/**
 * Global logger for general use.
 */
export const logger = createLogger('App')

export default Logger

