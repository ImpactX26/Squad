// NEXT_PUBLIC_* values are inlined at build time, so each must be referenced literally.
function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`${name} is not set. Copy web/.env.local.example to web/.env.local.`);
  }
  return value;
}

export const env = {
  apiUrl: required("NEXT_PUBLIC_API_URL", process.env.NEXT_PUBLIC_API_URL),
  wsUrl: required("NEXT_PUBLIC_WS_URL", process.env.NEXT_PUBLIC_WS_URL),
  companyName: required("NEXT_PUBLIC_COMPANY_NAME", process.env.NEXT_PUBLIC_COMPANY_NAME),
};
