// Token, dollar and wall-clock accounting across a run. Cache reads bill at 0.1x input, cache writes at 1.25x.
export interface Usage { inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheWriteTokens: number }

export class Budget {
  inputTokens = 0;
  outputTokens = 0;
  cacheReadTokens = 0;
  cacheWriteTokens = 0;
  usd = 0;
  readonly startedAt: number;
  constructor(private priceInPerM: number, private priceOutPerM: number, private now: () => number = Date.now, prior?: Partial<Budget>) {
    this.startedAt = now();
    if (prior) Object.assign(this, { inputTokens: prior.inputTokens ?? 0, outputTokens: prior.outputTokens ?? 0, cacheReadTokens: prior.cacheReadTokens ?? 0, cacheWriteTokens: prior.cacheWriteTokens ?? 0, usd: prior.usd ?? 0 });
  }
  add(u: Usage, priceScale = 1): number { // priceScale: a cheaper model's price relative to the main one
    const usd = priceScale * ((u.inputTokens + u.cacheReadTokens * 0.1 + u.cacheWriteTokens * 1.25) * this.priceInPerM + u.outputTokens * this.priceOutPerM) / 1e6;
    this.inputTokens += u.inputTokens;
    this.outputTokens += u.outputTokens;
    this.cacheReadTokens += u.cacheReadTokens;
    this.cacheWriteTokens += u.cacheWriteTokens;
    this.usd += usd;
    return usd;
  }
  // Cache reads are cheap and repeat the same prefix every call: they do not count against the token cap.
  get billedTokens() { return this.inputTokens + this.outputTokens + this.cacheWriteTokens; }
  get minutes() { return (this.now() - this.startedAt) / 60000; }
  toJSON() { return { inputTokens: this.inputTokens, outputTokens: this.outputTokens, cacheReadTokens: this.cacheReadTokens, cacheWriteTokens: this.cacheWriteTokens, usd: this.usd }; }
}
