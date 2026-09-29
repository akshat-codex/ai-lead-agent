/**
 * Client-side heuristic NL -> filter extraction. Not an LLM. This is an
 * explicit stopgap: a real backend NL-parse endpoint can replace this file's
 * implementation later without changing the calling component's contract —
 * every extraction still goes through a mandatory "review before confirm"
 * step, regardless of how the extraction itself is produced.
 *
 * Every result carries confidence "low" and the substring that matched, so
 * the UI can show exactly why something was suggested rather than presenting
 * it as a confident, verified fact.
 */
import { CUSTOM_FILTER_KEY, newClientId } from "./types";
import type { ExtractedFilter, FilterDefinition } from "./types";

interface Rule {
  key: string;
  pattern: RegExp;
  toValue: (match: RegExpMatchArray) => ExtractedFilter["value"];
}

// A comma-grouped or plain integer, e.g. "1,000" or "300" — never assumes a
// specific number of digits, so it works for any magnitude.
const _INT = "\\d{1,3}(?:,\\d{3})*|\\d+";

const EMPLOYEE_RANGE_RULE: Rule = {
  key: "employee_range",
  pattern: new RegExp(`(${_INT})\\s*(?:[-–—]|to)\\s*(${_INT})\\s*employees`, "i"),
  toValue: (m) => ({ min: Number(m[1].replace(/,/g, "")), max: Number(m[2].replace(/,/g, "")) }),
};

// Revenue phrased as a currency-and-magnitude range. Two orderings are
// both real, common phrasings and neither is preferred over the other:
//   - number-range THEN the word "revenue": "$10M-$500M revenue"
//   - the word "revenue" THEN the number-range: "revenue $10M-$500M",
//     "estimated revenue $10M-$500M"
// The magnitude suffix (K/M/B) is generic — never assumes a specific
// currency or scale, only that both bounds share a common notation. A
// bound may omit its own "$" (the second number in "$10M-500M" is still
// a dollar bound), but each bound's own magnitude suffix is read
// independently since ranges are sometimes written with differing units
// (rare, but never guessed here — if a suffix is missing on one side,
// that side's raw digits are used as-is, never inferred from the other
// side).
const _MAGNITUDE: Record<string, number> = { k: 1_000, m: 1_000_000, b: 1_000_000_000 };
const _REVENUE_RANGE_NUMBERS = `\\$?(${_INT})(?:\\.\\d+)?\\s*([kmb])?\\s*(?:[-–—]|to)\\s*\\$?(${_INT})(?:\\.\\d+)?\\s*([kmb])?`;
const REVENUE_RANGE_PATTERNS = [
  new RegExp(`${_REVENUE_RANGE_NUMBERS}\\s*(?:in )?revenue`, "i"), // "$10M-$500M revenue"
  new RegExp(`revenue\\s*(?:of |is |around |estimated )*${_REVENUE_RANGE_NUMBERS}`, "i"), // "estimated revenue $10M-$500M"
];

function toRevenueValue(m: RegExpMatchArray): ExtractedFilter["value"] {
  const toNumber = (raw: string, suffix: string | undefined) => {
    const base = Number(raw.replace(/,/g, ""));
    const mult = suffix ? _MAGNITUDE[suffix.toLowerCase()] : undefined;
    return mult ? base * mult : base;
  };
  return { min: toNumber(m[1], m[2]), max: toNumber(m[3], m[4]) };
}

/** Tries each revenue phrasing in turn and returns the first match — a
 * plain helper (not a single-pattern Rule, since revenue genuinely has
 * more than one common word order) so extractFiltersFromDescription can
 * stay a simple `if (match) {...}` like every other rule below. */
function matchRevenueRange(text: string): RegExpMatchArray | null {
  for (const pattern of REVENUE_RANGE_PATTERNS) {
    const match = text.match(pattern);
    if (match) return match;
  }
  return null;
}

// Phrases that introduce a NEGATIVE clause — anything they govern must
// never become a positive criterion. Generic trigger words, never tied to
// any specific industry/term; matches "exclude", "excluding", "not
// including", "except", "excludes". Deliberately does not try to detect
// the END of an exclusion clause via punctuation alone (a comma inside a
// list like "exclude hospitals, agencies, and staffing firms" must not
// end the clause early) — see extractExclusionSpans's own comment for how
// the clause's extent is actually bounded.
const EXCLUSION_TRIGGER = /\b(?:excluding|exclude[sd]?|except(?:\s+for)?|not\s+including)\b/gi;

// Where a new, unrelated positive clause resumes after an exclusion list —
// generic connective phrases, never a specific term. Stops the exclusion
// clause's span so a positive requirement stated later in the same
// sentence ("...excluding hospitals, but require 50-200 employees") is
// never swallowed into the exclusion list.
const EXCLUSION_CLAUSE_END = /[.;]|\bthat\s+(?:are|have|require)\b|\bwith\s+\d/i;

// A sentence surviving to the end of extraction (after every structured
// rule above has already masked out what it recognized) counts as a real
// leftover REQUIREMENT — not just punctuation/connective debris from a
// sentence whose actual content was already captured — when at least this
// many consecutive word characters remain. 12 is deliberately generic
// (not tuned to any specific phrase): short debris like "and are" or a
// lone "." never reaches it, while an actual sentence fragment like
// "companies that actually sell their own consumer products" easily does.
const _MEANINGFUL_REQUIREMENT = /\w[\w\s/-]{11,}\w/;

const GEOGRAPHY_TERMS: Record<string, string> = {
  "united states": "United States",
  "u\\.s\\.": "United States",
  "\\bus\\b": "United States",
  "european union": "European Union",
  "\\beu\\b": "European Union",
  gcc: "GCC",
  uk: "United Kingdom",
  "united kingdom": "United Kingdom",
  canada: "Canada",
  australia: "Australia",
  singapore: "Singapore",
  uae: "UAE",
  "saudi arabia": "Saudi Arabia",
  qatar: "Qatar",
};

const INDUSTRY_TERMS: Record<string, string> = {
  d2c: "D2C",
  "direct[- ]to[- ]consumer": "D2C",
  fmcg: "FMCG",
  healthcare: "Healthcare",
  ott: "OTT Platforms",
  microdrama: "Microdrama Companies",
  saas: "SaaS",
};

const TITLE_TERMS: Record<string, string> = {
  founder: "Founder",
  "co[- ]founder": "Co-Founder",
  ceo: "CEO",
  cmo: "CMO",
  "marketing head": "Marketing Head",
  "vp marketing": "VP Marketing",
  "head of growth": "Head of Growth",
  "director of marketing": "Director of Marketing",
  "marketing leaders?": "Marketing Head",
};

const FUNDING_TERMS: Record<string, string> = {
  "recently raised": "Recently raised",
  "raised funding": "Recently raised",
  "series [a-e]": "Recently raised",
};

const HIRING_TERMS: Record<string, string> = {
  "hiring (?:for )?marketing": "Marketing",
  "hiring marketing leaders": "Marketing",
};

interface Span {
  start: number;
  end: number;
  /** Where this span's trigger phrase ITSELF ends, i.e. the start of the
   * actual excluded-items list — "Exclude agencies, ..." -> triggerEnd
   * points just after "Exclude ". Used to split the list generically
   * without re-matching the trigger word as if it were a list item. */
  triggerEnd: number;
}

/** Every stretch of `text` governed by an exclusion trigger phrase (see
 * EXCLUSION_TRIGGER's own comment), from the trigger word itself to the
 * next clause boundary (EXCLUSION_CLAUSE_END) or end of string. Generic —
 * detects the STRUCTURE of a negative clause, never any specific term
 * inside it, so it works identically regardless of which industry/title/
 * geography term is being excluded. */
export function extractExclusionSpans(text: string): Span[] {
  const spans: Span[] = [];
  EXCLUSION_TRIGGER.lastIndex = 0;
  let triggerMatch: RegExpExecArray | null;
  while ((triggerMatch = EXCLUSION_TRIGGER.exec(text)) !== null) {
    const start = triggerMatch.index;
    const triggerEnd = EXCLUSION_TRIGGER.lastIndex;
    const rest = text.slice(triggerEnd);
    const endMatch = rest.match(EXCLUSION_CLAUSE_END);
    const end = endMatch && endMatch.index !== undefined ? triggerEnd + endMatch.index : text.length;
    spans.push({ start, end, triggerEnd });
  }
  return spans;
}

// Splits an exclusion clause's own item list on commas and "and"/"or"
// connectives — generic list-splitting, never tied to any specific
// business type or industry. Each resulting segment is trimmed and
// stripped of a leading connective word left over from the split (e.g.
// splitting "X, and Y" on "," alone leaves "and Y"); a segment that's
// only stray punctuation/whitespace is dropped.
const _LIST_SEPARATOR = /,|\band\b|\bor\b/i;
const _LEADING_CONNECTIVE = /^(?:and|or)\s+/i;

/** Every comma/and/or-separated item in an exclusion clause's own text,
 * taken VERBATIM (never reduced to a known term-map label) — this is what
 * lets "SaaS vendors", "marketing firms", "staffing firms" etc. all
 * survive as meaningful, multi-word exclusion entries even though none of
 * them (except "SaaS" itself) has any entry in this file's term maps.
 * Works identically for any list of excluded business types/industries —
 * nothing here is specific to D2C or any other ICP. */
function splitExclusionListItems(text: string, span: Span): string[] {
  const clauseText = text.slice(span.triggerEnd, span.end);
  return clauseText
    .split(_LIST_SEPARATOR)
    .map((item) => item.replace(_LEADING_CONNECTIVE, "").trim())
    .filter((item) => item.length > 1);
}

function isWithinAnySpan(index: number, spans: Span[]): boolean {
  return spans.some((s) => index >= s.start && index < s.end);
}

/** Matches a term map against `text`, but SKIPS any match whose position
 * falls inside an exclusion span — this is the generic mechanism that
 * keeps a term stated only inside "Exclude ... X" from ever becoming a
 * positive criterion for X, regardless of what X is. A term matched both
 * inside and outside an exclusion span (e.g. mentioned twice) still
 * counts as a genuine positive match, since a real, separate positive
 * mention exists outside the exclusion. */
function extractFromTermMap(
  text: string,
  key: string,
  terms: Record<string, string>,
  exclusionSpans: Span[] = [],
): ExtractedFilter[] {
  const found: { value: string; matchedText: string }[] = [];
  for (const [pattern, label] of Object.entries(terms)) {
    const re = new RegExp(pattern, "gi");
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
      if (!isWithinAnySpan(match.index, exclusionSpans)) {
        found.push({ value: label, matchedText: match[0] });
        break; // one positive match per term is enough to include it
      }
      if (match[0].length === 0) re.lastIndex++; // guard against zero-width patterns looping forever
    }
  }
  if (found.length === 0) return [];
  return [
    {
      id: newClientId(),
      key,
      operator: "in",
      value: [...new Set(found.map((f) => f.value))],
      source: "nl_extraction",
      confidence: "low",
      matchedText: found.map((f) => f.matchedText).join(", "),
    },
  ];
}

// Title-cases each word of a free-text exclusion item for display
// consistency with the term-map labels ("SaaS Vendors", "Marketing
// Firms") — purely cosmetic, never changes which words are present. A
// segment that's already an exact known term-map label (e.g. plain
// "SaaS") is left as that label verbatim instead (see
// extractExclusionTerms), so this only applies to the multi-word,
// not-otherwise-recognized items.
function titleCase(value: string): string {
  return value.replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Everything inside an exclusion clause's own comma/and/or-separated
 * list becomes a negative "exclusions" entry — generic list-splitting
 * (splitExclusionListItems), never limited to terms this file happens to
 * have a label for. A segment that exactly matches a known term-map
 * label (any of industry/geography/title/funding/hiring — e.g. a
 * standalone "SaaS") uses that label's canonical casing; every other
 * segment (e.g. "SaaS vendors", "marketing firms", "staffing firms") is
 * preserved AS ITS OWN, verbatim (title-cased) exclusion entry rather
 * than being reduced to a partial known-term match or dropped — this is
 * what keeps "SaaS vendors" as one meaningful exclusion instead of
 * silently losing "vendors" or being discarded because the full phrase
 * isn't a recognized term. Works identically for any list of excluded
 * business types, never hardcoded to any specific ICP's exclusion list. */
function extractExclusionTerms(text: string, exclusionSpans: Span[], termMaps: Record<string, string>[]): ExtractedFilter[] {
  if (exclusionSpans.length === 0) return [];

  const knownLabelsByLowerValue = new Map<string, string>();
  for (const terms of termMaps) {
    for (const label of Object.values(terms)) {
      knownLabelsByLowerValue.set(label.toLowerCase(), label);
    }
  }

  const values: string[] = [];
  const matchedTexts: string[] = [];
  for (const span of exclusionSpans) {
    for (const item of splitExclusionListItems(text, span)) {
      const knownLabel = knownLabelsByLowerValue.get(item.toLowerCase());
      values.push(knownLabel ?? titleCase(item));
      matchedTexts.push(item);
    }
  }

  if (values.length === 0) return [];
  return [
    {
      id: newClientId(),
      key: "exclusions",
      operator: "not_in",
      value: [...new Set(values)],
      source: "nl_extraction",
      confidence: "low",
      matchedText: matchedTexts.join(", "),
    },
  ];
}

export interface ExtractionResult {
  filters: ExtractedFilter[];
  /** Fragments of the description that didn't map to any known filter. */
  unmatched: string[];
}

export function extractFiltersFromDescription(
  text: string,
  catalog: FilterDefinition[],
): ExtractionResult {
  const catalogKeys = new Set(catalog.map((d) => d.key));
  const filters: ExtractedFilter[] = [];
  const matchedSpans: string[] = [];

  // Computed ONCE, up front, and threaded through every positive
  // term-extraction call below — this is the generic mechanism that keeps
  // a term stated only inside "Exclude ... X" (any X, any term map) from
  // ever becoming a positive criterion, and separately promotes it into
  // the exclusions filter (see extractExclusionTerms below).
  const exclusionSpans = extractExclusionSpans(text);

  if (catalogKeys.has("industry")) {
    const found = extractFromTermMap(text, "industry", INDUSTRY_TERMS, exclusionSpans);
    filters.push(...found);
    found.forEach((f) => f.matchedText && matchedSpans.push(f.matchedText));
  }
  if (catalogKeys.has("geography")) {
    const found = extractFromTermMap(text, "geography", GEOGRAPHY_TERMS, exclusionSpans);
    filters.push(...found);
    found.forEach((f) => f.matchedText && matchedSpans.push(f.matchedText));
  }
  if (catalogKeys.has("allowed_titles")) {
    const found = extractFromTermMap(text, "allowed_titles", TITLE_TERMS, exclusionSpans);
    filters.push(...found);
    found.forEach((f) => f.matchedText && matchedSpans.push(f.matchedText));
  }
  if (catalogKeys.has("funding")) {
    const found = extractFromTermMap(text, "funding", FUNDING_TERMS, exclusionSpans);
    filters.push(...found);
    found.forEach((f) => f.matchedText && matchedSpans.push(f.matchedText));
  }
  if (catalogKeys.has("hiring")) {
    const found = extractFromTermMap(text, "hiring", HIRING_TERMS, exclusionSpans);
    filters.push(...found);
    found.forEach((f) => f.matchedText && matchedSpans.push(f.matchedText));
  }
  if (catalogKeys.has("exclusions")) {
    const found = extractExclusionTerms(text, exclusionSpans, [INDUSTRY_TERMS, GEOGRAPHY_TERMS, TITLE_TERMS, FUNDING_TERMS, HIRING_TERMS]);
    filters.push(...found);
    // NOT pushed onto matchedSpans (see below): an exclusion clause's
    // list items are stripped from `remainder` by POSITION (the span's
    // own start/end), not by re-finding each item's text — a
    // comma/and-separated list's items don't reappear in the source text
    // in the same joined form extractExclusionTerms returns them in, so a
    // naive string search would silently fail to strip them and they'd
    // wrongly resurface as "unmatched" fragments.
  }
  if (catalogKeys.has("employee_range")) {
    const match = text.match(EMPLOYEE_RANGE_RULE.pattern);
    if (match) {
      filters.push({
        id: newClientId(),
        key: "employee_range",
        operator: "range",
        value: EMPLOYEE_RANGE_RULE.toValue(match),
        source: "nl_extraction",
        confidence: "low",
        matchedText: match[0],
      });
      matchedSpans.push(match[0]);
    }
  }
  if (catalogKeys.has("revenue")) {
    const match = matchRevenueRange(text);
    if (match) {
      filters.push({
        id: newClientId(),
        key: "revenue",
        operator: "range",
        value: toRevenueValue(match),
        source: "nl_extraction",
        confidence: "low",
        matchedText: match[0],
      });
      matchedSpans.push(match[0]);
    }
  }

  // Whatever's left, minus matched spans and common connective words, is
  // surfaced as "unmatched" so the user knows the extractor didn't
  // understand it rather than assuming it was redundant.
  let remainder = text;

  // Exclusion clauses are masked by POSITION (see the exclusions branch
  // above for why a string search can't reliably find a comma/and-list's
  // items again) — but only when the "exclusions" filter was actually
  // populated (catalog supports it AND real items were found); otherwise
  // an exclusion clause the catalog can't record must still surface as
  // unmatched, not be silently discarded.
  const exclusionsWerePopulated = filters.some((f) => f.key === "exclusions");
  if (exclusionsWerePopulated) {
    const chars = [...remainder];
    for (const span of exclusionSpans) {
      for (let i = span.start; i < span.end && i < chars.length; i++) chars[i] = " ";
    }
    remainder = chars.join("");
  }

  for (const span of matchedSpans) {
    remainder = remainder.replace(new RegExp(escapeRegExp(span), "i"), " ");
  }

  // Requirement 4 — a trailing business-model/requirement sentence (e.g.
  // "Find companies that actually sell their own consumer products
  // through ecommerce/DTC channels") has no dedicated structured filter
  // in today's catalog, but must never be silently dropped: when the
  // catalog exposes the generic "custom" escape hatch, an ENTIRE
  // ORIGINAL sentence that no structured rule above claimed ANY part of
  // becomes one custom requirement filter (using the real, unmodified
  // sentence text — never a partially-masked fragment); otherwise it
  // still surfaces via the normal unmatched list below. This is generic —
  // it never looks for "ecommerce"/"DTC" specifically, only for a whole
  // sentence substantial enough (_MEANINGFUL_REQUIREMENT) that no rule
  // above recognized anything in it at all, which is exactly what
  // distinguishes a genuine leftover requirement from a sentence whose
  // content was already captured (that sentence's masked-out remnants are
  // mostly punctuation/connectives and never pass the length check).
  if (catalogKeys.has(CUSTOM_FILTER_KEY)) {
    const maskedSentences = remainder.split(/(?<=[.!?])\s+/);
    const originalSentences = text.split(/(?<=[.!?])\s+/);
    for (let i = 0; i < originalSentences.length; i++) {
      const originalSentence = originalSentences[i].trim();
      const maskedSentence = (maskedSentences[i] ?? "").trim();
      if (!_MEANINGFUL_REQUIREMENT.test(maskedSentence)) continue;
      // The masked version must still read as SUBSTANTIALLY the same
      // sentence (nothing inside it was claimed by any rule above) —
      // comparing lengths (ignoring whitespace collapse from masking)
      // is a simple, generic proxy for "no rule matched anything here."
      const maskedWordChars = maskedSentence.replace(/\s+/g, "").length;
      const originalWordChars = originalSentence.replace(/\s+/g, "").length;
      if (maskedWordChars < originalWordChars * 0.8) continue;
      filters.push({
        id: newClientId(),
        key: CUSTOM_FILTER_KEY,
        operator: "contains",
        value: originalSentence,
        label: originalSentence.length > 60 ? `${originalSentence.slice(0, 57)}...` : originalSentence,
        source: "nl_extraction",
        confidence: "low",
        matchedText: originalSentence,
      });
      remainder = remainder.replace(maskedSentences[i] ?? "", " ");
    }
  }

  const unmatched = remainder
    .split(/[.,;]|(?:\band\b)/i)
    .map((s) => s.trim())
    .filter((s) => s.length > 3 && !/^(find|with|that|are|the|for)$/i.test(s));

  return { filters, unmatched };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export { CUSTOM_FILTER_KEY };
