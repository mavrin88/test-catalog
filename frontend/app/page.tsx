import { TelegramBoot } from "./telegram";

export const dynamic = "force-dynamic";

type Product = {
  id: string;
  name: string;
  image_url: string;
};

async function loadProducts(): Promise<Product[] | null> {
  const base = (process.env.API_INTERNAL_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
  try {
    const response = await fetch(`${base}/api/products`, { cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()) as Product[];
  } catch {
    return null;
  }
}

export default async function HomePage() {
  const products = await loadProducts();

  return (
    <main>
      <TelegramBoot />
      <h1>Каталог</h1>
      {products === null && <p>Не удалось загрузить товары.</p>}
      {products?.length === 0 && <p>Пока пусто.</p>}
      {products && products.length > 0 && (
        <ul>
          {products.map((item) => (
            <li key={item.id}>
              <img src={item.image_url} alt={item.name} />
              <span>{item.name}</span>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
