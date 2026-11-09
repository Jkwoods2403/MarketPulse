/**
 * The label that identifies a non-production deployment. Pure, with no imports (see
 * chart-range.ts). Production carries no label; every other environment is always named, so a
 * staging or development deployment can never be mistaken for production.
 */
export function environmentLabel(environment: string): string | null {
  if (environment === "production") return null;
  return environment.charAt(0).toUpperCase() + environment.slice(1);
}
