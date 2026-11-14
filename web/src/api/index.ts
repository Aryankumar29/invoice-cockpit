import { httpApi, type Api } from "./client";
import { mockApi } from "./mock";

export const MOCK = import.meta.env.VITE_MOCK === "1";
export const api: Api = MOCK ? mockApi : httpApi;
export * from "./types";
export { ApiError } from "./client";
