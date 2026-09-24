/**
 * Identity module. Every call goes through the one HTTP client (§106).
 *
 * NOTE: `login` receives a new token pair and writes it through the client's token
 * store (never directly into localStorage from a component).
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import { setTokens } from '@/api/tokenStore'
import type {
  AddressPayload,
  CurrentUser,
  LoginRequest,
  LoginResponse,
  PermissionResponse,
} from '@/types/api-contract'
import type { Address, Paged } from '@/types/domain'

export const authApi = {
  async login(payload: LoginRequest): Promise<LoginResponse> {
    const result = await httpClient.post<LoginResponse>(API.auth.login, payload)
    setTokens({ accessToken: result.access_token })
    return result
  },

  async logout(): Promise<void> {
    await httpClient.post<void>(API.auth.logout, {})
  },

  async me(): Promise<CurrentUser> {
    return httpClient.get<CurrentUser>(API.auth.me)
  },

  /** Permission codes are a UX hint only; the backend is the authority (§104). */
  async permissions(): Promise<PermissionResponse> {
    return httpClient.get<PermissionResponse>(API.auth.permissions)
  },
}

export const addressApi = {
  async list(): Promise<Paged<Address>> {
    return httpClient.get<Paged<Address>>(API.addresses.list)
  },

  async create(payload: AddressPayload): Promise<Address> {
    return httpClient.post<Address>(API.addresses.create, payload)
  },

  async update(id: number, payload: Partial<AddressPayload>): Promise<Address> {
    return httpClient.put<Address>(API.addresses.update(id), payload)
  },

  async remove(id: number): Promise<void> {
    await httpClient.delete<void>(API.addresses.remove(id))
  },

  async setDefault(id: number): Promise<Address> {
    return httpClient.post<Address>(API.addresses.setDefault(id), {})
  },
}
