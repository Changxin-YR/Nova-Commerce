/**
 * Catalog module: consumer reads + merchant writes (§98, §99).
 *
 * Merchant writes are TASK-BASED: `publish` / `unpublish` are explicit intents,
 * never a bare status patch.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  ProductPayload,
  ProductQuery,
  SkuPayload,
} from '@/types/api-contract'
import type { Brand, Category, Paged, Product, ProductSummary, Sku } from '@/types/domain'

interface UploadedProductImage {
  id: number
  url: string | null
  alt: string | null
  role: 'PRIMARY' | 'GALLERY' | 'DETAIL'
  sort_order: number
}

export const catalogApi = {
  async searchProducts(query: ProductQuery = {}): Promise<Paged<ProductSummary>> {
    return httpClient.get<Paged<ProductSummary>>(API.catalog.products, { params: query })
  },

  async product(id: string | number): Promise<Product> {
    return httpClient.get<Product>(API.catalog.productDetail(id))
  },

  async categories(): Promise<Paged<Category>> {
    return httpClient.get<Paged<Category>>(API.catalog.categories)
  },

  async brands(): Promise<Paged<Brand>> {
    return httpClient.get<Paged<Brand>>(API.catalog.brands)
  },
}

export const catalogAdminApi = {
  async products(query: ProductQuery = {}): Promise<Paged<ProductSummary>> {
    return httpClient.get<Paged<ProductSummary>>(API.catalog.adminProducts, { params: query })
  },

  async product(id: string | number): Promise<Product> {
    return httpClient.get<Product>(API.catalog.adminProduct(id))
  },

  async create(payload: ProductPayload): Promise<Product> {
    return httpClient.post<Product>(API.catalog.adminProducts, payload)
  },

  async update(id: string | number, payload: Partial<ProductPayload>): Promise<Product> {
    return httpClient.put<Product>(API.catalog.adminProduct(id), payload)
  },

  /** Task endpoint (§99): publishing is an intent, not a `PATCH {status}`. */
  async publish(id: number): Promise<Product> {
    return httpClient.post<Product>(API.catalog.publish(id), {})
  },

  async unpublish(id: number): Promise<Product> {
    return httpClient.post<Product>(API.catalog.unpublish(id), {})
  },

  async addSku(productId: string | number, payload: SkuPayload): Promise<Sku> {
    return httpClient.post<Sku>(API.catalog.skus(productId), payload)
  },

  async uploadImage(productId: number, file: File, role: 'PRIMARY' | 'GALLERY' | 'DETAIL'): Promise<UploadedProductImage> {
    const form = new FormData()
    form.append('file', file)
    form.append('role', role)
    return httpClient.upload<UploadedProductImage>(API.catalog.uploadImage(productId), form)
  },
}
