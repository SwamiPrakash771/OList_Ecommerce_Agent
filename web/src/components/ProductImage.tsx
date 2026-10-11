// The dataset has no photos, so each product gets a generated image:
// a category icon on a soft gradient whose hue is derived from the product id.

import {
  Armchair, Baby, Bath, Book, Camera, Car, Cookie, Dumbbell, Flower2, Gamepad2, Gift, Hammer,
  Headphones, Heart, Home, Laptop, Music, Package, PawPrint, Shirt, ShoppingBag, Smartphone,
  Sparkles, Tv, UtensilsCrossed, Watch, Wine, type LucideIcon,
} from 'lucide-react'
import { hueFor } from '../lib'

const ICONS: [RegExp, LucideIcon][] = [
  [/watch/, Watch],
  [/health|beauty|perfum/, Sparkles],
  [/bed|bath/, Bath],
  [/furniture|decor|office|mattress/, Armchair],
  [/computer|pc_|tablet/, Laptop],
  [/telephon|phone/, Smartphone],
  [/electronic|audio|cine/, Headphones],
  [/tv|appliances/, Tv],
  [/sport|leisure|fitness/, Dumbbell],
  [/toy|game|console/, Gamepad2],
  [/baby|diaper/, Baby],
  [/pet/, PawPrint],
  [/garden|flower/, Flower2],
  [/tool|construction|costruction/, Hammer],
  [/auto/, Car],
  [/book/, Book],
  [/music|cds|dvd/, Music],
  [/photo|camera/, Camera],
  [/fashion|cloth|shoe|bags/, Shirt],
  [/food|drink|wine/, Wine],
  [/kitchen|housewares|portable_kitchen|la_cuisine/, UtensilsCrossed],
  [/home|housewa/, Home],
  [/gift|christmas|party/, Gift],
  [/cookie|snack/, Cookie],
  [/health/, Heart],
  [/market|luggage|stationery/, ShoppingBag],
]

export function iconFor(category: string): LucideIcon {
  return ICONS.find(([re]) => re.test(category))?.[1] ?? Package
}

export default function ProductImage({
  productId,
  category,
  className = '',
  iconSize = 56,
}: {
  productId: string
  category: string
  className?: string
  iconSize?: number
}) {
  const Icon = iconFor(category)
  const hue = hueFor(productId)
  return (
    <div
      className={`flex items-center justify-center overflow-hidden ${className}`}
      style={{
        background: `linear-gradient(135deg, hsl(${hue} 70% 94%), hsl(${(hue + 40) % 360} 65% 86%))`,
      }}
      aria-hidden
    >
      <Icon size={iconSize} strokeWidth={1.4} style={{ color: `hsl(${hue} 45% 38%)` }} />
    </div>
  )
}
