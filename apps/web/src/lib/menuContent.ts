/** Reject non-dish rows from both cached and newly collected menus. */
export function isIndividualFoodItem(name: string): boolean {
  const value = name.trim().toLowerCase();
  if (/^menus?\b/.test(value) || /\b(?:added|uploaded|updated)\s+by\b|\b(?:days?|weeks?|months?|years?)\s+ago\b/.test(value)) return false;
  if (/\b(meals?|combos?|bundles?|deals?|offers?|vouchers?|gift cards?)\b/.test(value)) return false;
  if (/^(?:menu|starters?|mains?|desserts?|sides?|beverages?|drinks?|order online|view menu|download menu)$/.test(value)) return false;
  if (/^(?:(?:iced|hot|black|green)\s+)?(?:coffee|tea|latte|cappuccino|espresso|water|coca.?cola|pepsi|sprite|fanta|soft drinks?|juice|cocktails?|wine|beer)(?:\s+\d+(?:\s?ml)?)?$/.test(value)) return false;
  return !/^(?:aed|usd|eur|gbp)?\s*\d+(?:\.\d+)?\s+allergens?$/i.test(value);
}
