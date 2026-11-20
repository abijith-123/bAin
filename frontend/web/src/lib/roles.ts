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

const demoTokens = {
  doctor: import.meta.env.VITE_BAIN_TOKEN_DOCTOR,
  nurse: import.meta.env.VITE_BAIN_TOKEN_NURSE,
  pharmacist: import.meta.env.VITE_BAIN_TOKEN_PHARMACIST,
  billing_specialist: import.meta.env.VITE_BAIN_TOKEN_BILLING_SPECIALIST,
  transport_staff: import.meta.env.VITE_BAIN_TOKEN_TRANSPORT_STAFF,
  housekeeping: import.meta.env.VITE_BAIN_TOKEN_HOUSEKEEPING,
  admin: import.meta.env.VITE_BAIN_TOKEN_ADMIN,
};

/**
 * Synthetic demonstration identities for local hackathon simulation.
 * The FastAPI backend validates the bearer token and enforces all permissions and redaction.
 */
export const DEMO_ROLES: DemoRoleProfile[] = [
  {
    role: "doctor",
    label: "Attending Physician",
    shortLabel: "Doctor",
    demoToken: demoTokens.doctor,
    scopeLabel: "Clinical PHI Authorized",
    phiPolicy: "clinical_phi",
    description: "Clinical clearance authority with full synthetic clinical summary visibility.",
  },
  {
    role: "nurse",
    label: "Discharge Nurse",
    shortLabel: "Nurse",
    demoToken: demoTokens.nurse,
    scopeLabel: "Clinical PHI Authorized",
    phiPolicy: "clinical_phi",
    description: "Clinical handoff visibility and authorized patient departure confirmation.",
  },
  {
    role: "pharmacist",
    label: "Clinical Pharmacist",
    shortLabel: "Pharmacist",
    demoToken: demoTokens.pharmacist,
    scopeLabel: "Pharmacy Scope",
    phiPolicy: "departmental",
    description: "Resolves medication stockouts and confirms Pharmacy milestone completion.",
  },
  {
    role: "billing_specialist",
    label: "Billing Specialist",
    shortLabel: "Billing",
    demoToken: demoTokens.billing_specialist,
    scopeLabel: "Insurance Scope",
    phiPolicy: "departmental",
    description: "Resolves ERR_AUTH_409 payer exceptions and confirms Insurance clearance.",
  },
  {
    role: "transport_staff",
    label: "Patient Transport",
    shortLabel: "Transport",
    demoToken: demoTokens.transport_staff,
    scopeLabel: "No Clinical PHI",
    phiPolicy: "operational_only",
    description: "Confirms actual patient departure; clinical notes and diagnoses are server-redacted.",
  },
  {
    role: "housekeeping",
    label: "Environmental Services",
    shortLabel: "Housekeeping",
    demoToken: demoTokens.housekeeping,
    scopeLabel: "Room/Bed Ops Only",
    phiPolicy: "operational_only",
    description: "Dispatches cleaning and marks bed ready after departure; zero clinical PHI exposure.",
  },
  {
    role: "admin",
    label: "Operations Admin",
    shortLabel: "Admin",
    demoToken: demoTokens.admin,
    scopeLabel: "Operational Overview",
    phiPolicy: "departmental",
    description: "Operational oversight and audit visibility; cannot bypass specialist or clinical gates.",
  },
];

export function getRoleProfile(role: Role): DemoRoleProfile {
  return DEMO_ROLES.find((r) => r.role === role) ?? DEMO_ROLES[0];
}
