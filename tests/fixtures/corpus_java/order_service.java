/*
 * License header: for fixture purposes only.
 */
package com.example.order;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 订单应用服务：创建、取消与状态查询。
 */
@Service
@Transactional(rollbackFor = Exception.class)
public class OrderService {

    /** 幂等键缓存：requestId -> orderId。 */
    private final Map<String, Long> idempotencyCache = new ConcurrentHashMap<>();

    private final OrderRepository orderRepository;
    private static final int MAX_RETRY = 3;

    static {
        System.setProperty("order.service.loaded", "true");
    }

    /**
     * 构造注入仓储。
     */
    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    /**
     * 创建订单：重复的 requestId 返回既有订单（幂等）。
     *
     * @param requestId 客户端幂等键
     */
    public Long createOrder(String requestId, List<OrderItem> items) {
        Long existing = idempotencyCache.get(requestId);
        if (existing != null) {
            return existing;
        }
        Long orderId = orderRepository.save(items);
        idempotencyCache.put(requestId, orderId);
        return orderId;
    }

    // 泛型辅助：按键聚合
    private <K, V> Map<K, List<V>> groupBy(List<V> values, KeyFn<K, V> fn) {
        Map<K, List<V>> grouped = new ConcurrentHashMap<>();
        for (V value : values) {
            grouped.computeIfAbsent(fn.key(value), k -> new java.util.ArrayList<>()).add(value);
        }
        return grouped;
    }

    /**
     * 内部重试策略（嵌套类）。
     */
    public static class RetryPolicy {

        private final int maxAttempts;

        public RetryPolicy(int maxAttempts) {
            this.maxAttempts = maxAttempts;
        }

        public boolean shouldRetry(int attempt) {
            return attempt < maxAttempts;
        }
    }
}
