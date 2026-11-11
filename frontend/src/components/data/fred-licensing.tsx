import { FRED_LICENSING_STATUS } from "@/lib/fred-status";

/** Link to the FRED section of docs/data-sources.md in the public repository. */
export function FredLicensingLink() {
  return (
    <a
      href={FRED_LICENSING_STATUS.referenceUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline decoration-dotted"
    >
      {FRED_LICENSING_STATUS.reference} (FRED section)
    </a>
  );
}

/**
 * Footer for a section whose FRED series are unavailable. It replaces the source attribution:
 * no FRED values are shown, so nothing is attributed to FRED.
 */
export function FredLicensingNotice() {
  return (
    <p className="text-[11px] leading-4 text-faint">
      {FRED_LICENSING_STATUS.summary} Licensing details: <FredLicensingLink />.
    </p>
  );
}
