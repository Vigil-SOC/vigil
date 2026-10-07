// Same host test the AI Config key form uses for an Ollama URL. A loopback
// host is this machine, so traffic to it does not leave the site.
const LOOPBACK_HOST = /^(https?:\/\/)?(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i

export function urlIsLoopback(url: string): boolean {
  return LOOPBACK_HOST.test(url.trim())
}
