<script lang="ts">
  // 线条图标（近似 SF Symbols 的粗细），跟随文字颜色。
  let { name, size = 16 }: { name: string; size?: number } = $props();
</script>

<svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  {#if name === "feed"}
    <path d="M3 12h3l2.5-6 4 12 3-9 1.5 3H21" />
  {:else if name === "symbols"}
    <path d="M8 6h12M8 12h12M8 18h12" /><circle cx="4" cy="6" r="1" fill="currentColor" /><circle cx="4" cy="12" r="1" fill="currentColor" /><circle cx="4" cy="18" r="1" fill="currentColor" />
  {:else if name === "stats"}
    <path d="M5 20V11M10 20V5M15 20v-7M20 20V9" />
  {:else if name === "alerts"}
    <path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15z" /><path d="M10 20.5a2 2 0 0 0 4 0" />
  {:else if name === "settings"}
    <circle cx="12" cy="12" r="3" /><path d="M12 2.8v2.4M12 18.8v2.4M4.2 7.5l2.1 1.2M17.7 15.3l2.1 1.2M4.2 16.5l2.1-1.2M17.7 8.7l2.1-1.2" /><circle cx="12" cy="12" r="7" />
  {:else if name === "search"}
    <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" />
  {:else if name === "back"}
    <path d="M15 5l-7 7 7 7" />
  {:else if name === "refresh"}
    <path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" />
  {:else if name === "thumbup"}
    <path d="M7 11v9H4v-9zM7 11l4-8c1.5 0 2.5 1 2.5 2.5V9h5a2 2 0 0 1 2 2.3l-1.2 6.5A2.5 2.5 0 0 1 16.8 20H7" />
  {:else if name === "thumbdown"}
    <path d="M7 13V4H4v9zM7 13l4 8c1.5 0 2.5-1 2.5-2.5V15h5a2 2 0 0 0 2-2.3l-1.2-6.5A2.5 2.5 0 0 0 16.8 4H7" />
  {/if}
</svg>
