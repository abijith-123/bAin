import type { Role } from "@/types/api";

export interface DemoRoleProfile {
  role: Role;
  label: string;
  shortLabel: string;
  demoToken: string;
  scopeLabel: string;
  phiPolicy: "clinical_phi" | "departmental" | "operational_only";
  description: string;
}

/**
 * Synthetic demonstration identities for local hackathon simulation.
 * The FastAPI backend validates the bearer token and enforces all permissions and redaction.
 */
export const DEMO_ROLES: DemoRoleProfile[] = [
  {
    role: "doctor",
    label: "Attending Physician",
    shortLabel: "Doctor",
    demoToken: "demo-token-doctor",
    scopeLabel: "Clinical PHI Authorized",
    phiPolicy: "clinical_phi",
    description: "Clinical clearance authority with full synthetic clinical summary visibility.",
  },
  {
    role: "nurse",
    label: "Discharge Nurse",
    shortLabel: "Nurse",
    demoToken: "demo-token-nurse",
    scopeLabel: "Clinical PHI Authorized",
    phiPolicy: "clinical_phi",
    description: "Clinical handoff visibility and authorized patient departure confirmation.",
  },
  {
    role: "pharmacist",
    label: "Clinical Pharmacist",
    shortLabel: "Pharmacist",
    demoToken: "demo-token-pharmacist",
    scopeLabel: "Pharmacy Scope",
    phiPolicy: "departmental",
    description: "Resolves medication stockouts and confirms Pharmacy milestone completion.",
  },
  {
    role: "billing_specialist",
    label: "Billing Specialist",
    shortLabel: "Billing",
    demoToken: "demo-token-billing",
    scopeLabel: "Insurance Scope",
    phiPolicy: "departmental",
    description: "Resolves ERR_AUTH_409 payer exceptions and confirms Insurance clearance.",
  },
  {
    role: "transport_staff",
    label: "Patient Transport",
    shortLabel: "Transport",
    demoToken: "demo-token-transport",
    scopeLabel: "No Clinical PHI",
    phiPolicy: "operational_only",
    description: "Confirms actual patient departure; clinical notes and diagnoses are server-redacted.",
  },
  {
    role: "housekeeping",
    label: "Environmental Services",
    shortLabel: "Housekeeping",
    demoToken: "demo-token-housekeeping",
    scopeLabel: "Room/Bed Ops Only",
    phiPolicy: "operational_only",
    description: "Dispatches cleaning and marks bed ready after departure; zero clinical PHI exposure.",
  },
  {
    role: "admin",
    label: "Operations Admin",
    shortLabel: "Admin",
    demoToken: "demo-token-admin",
    scopeLabel: "Operational Overview",
    phiPolicy: "departmental",
    description: "Operational oversight and audit visibility; cannot bypass specialist or clinical gates.",
  },
];

export function getRoleProfile(role: Role): DemoRoleProfile {
  return DEMO_ROLES.find((r) => r.role === role) ?? DEMO_ROLES[0];
}
