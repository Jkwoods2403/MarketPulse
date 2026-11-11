import { StateMessage } from "@/components/data/state-message";
import { MACRO_STATUS } from "@/lib/fred-status";

/** Link to the decision record (ADR-0015) in the public repository. */
export function MacroStatusLink() {
  return (
    <a
      href={MACRO_STATUS.referenceUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline decoration-dotted"
    >
      {MACRO_STATUS.reference}
    </a>
  );
}

/**
 * Why there is no macroeconomic data: FRED is retired and no direct source is integrated yet.
 * Nothing is requested, shown, or attributed in its place.
 */
export function MacroStatusMessage() {
  return (
    <StateMessage tone="unavailable" title={MACRO_STATUS.heading}>
      {MACRO_STATUS.summary} Decision record: <MacroStatusLink />.
    </StateMessage>
  );
}
