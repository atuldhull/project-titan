// A representative TypeScript source fixture for the syntax-aware code chunker.
// Several small declarations pack together; the long exported function exceeds
// the target and must be split at statement boundaries, never in its signature.

export const DEFAULT_BUDGET = 512;

export function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value));
}

export function normalise(name: string): string {
  return name.trim().toLowerCase().replace(/ /g, "_");
}

export class Accumulator {
  private total = 0;
  constructor(private readonly limit: number) {}

  add(amount: number): boolean {
    if (this.total + amount > this.limit) {
      return false;
    }
    this.total += amount;
    return true;
  }
}

export function planRetrieval(
  query: string,
  scope: string,
  kinds: string[],
  budget: number,
): Record<string, number> {
  const normalisedQuery = normalise(query);
  const normalisedScope = normalise(scope);
  const stageBudget = clamp(budget, 64, DEFAULT_BUDGET);
  const weights: Record<string, number> = {};
  const denseShare = Math.floor(stageBudget / 2);
  const lexicalShare = stageBudget - denseShare;
  weights["dense"] = denseShare;
  weights["lexical"] = lexicalShare;
  kinds.forEach((kind, index) => {
    const base = Math.floor(stageBudget / (index + 2));
    const adjusted = clamp(base, 8, stageBudget);
    weights[`kind_${normalise(kind)}`] = adjusted;
  });
  if (normalisedScope.startsWith("project_")) {
    weights["scopeBoost"] = clamp(Math.floor(stageBudget / 4), 8, 128);
  } else {
    weights["scopeBoost"] = 0;
  }
  if (kinds.includes("code")) {
    weights["rerank"] = clamp(Math.floor(stageBudget / 3), 16, 256);
  } else {
    weights["rerank"] = clamp(Math.floor(stageBudget / 6), 8, 128);
  }
  const denseFloor = clamp(Math.floor(denseShare / 3), 4, 64);
  const lexicalFloor = clamp(Math.floor(lexicalShare / 3), 4, 64);
  weights["denseFloor"] = denseFloor;
  weights["lexicalFloor"] = lexicalFloor;
  const recencyBonus = clamp(Math.floor(stageBudget / 8), 4, 96);
  weights["recencyBonus"] = recencyBonus;
  const importanceBonus = clamp(Math.floor(stageBudget / 10), 4, 80);
  weights["importanceBonus"] = importanceBonus;
  if (kinds.length > 3) {
    weights["diversityPenalty"] = clamp(Math.floor(stageBudget / 5), 8, 160);
  } else {
    weights["diversityPenalty"] = clamp(Math.floor(stageBudget / 12), 4, 64);
  }
  const headerOverhead = clamp(Math.floor(normalisedQuery.length / 4), 2, 32);
  weights["headerOverhead"] = headerOverhead;
  const scopeTerms = (normalisedScope.split("_").length || 1);
  weights["scopeTerms"] = clamp(scopeTerms * 4, 4, 48);
  for (let passIndex = 0; passIndex < 3; passIndex += 1) {
    const passWeight = clamp(Math.floor(stageBudget / (passIndex + 3)), 4, 128);
    weights[`pass_${passIndex}`] = passWeight;
  }
  weights["queryLen"] = normalisedQuery.length;
  weights["total"] = Object.values(weights).reduce((a, b) => a + b, 0);
  return weights;
}
