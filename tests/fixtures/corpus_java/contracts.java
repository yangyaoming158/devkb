package com.example.contract;

/**
 * 支付网关契约（接口 + 常量 + default 方法）。
 */
public interface PaymentGateway {

    /** 网关侧幂等重放错误码。 */
    int DUPLICATE_PAY_CODE = 40901;
    String CHANNEL = "rabbitmq";

    PaymentResult charge(String orderId, long amountCents);

    default boolean isDuplicate(int code) {
        return code == DUPLICATE_PAY_CODE;
    }
}

/**
 * 订单状态（枚举 + 方法）。
 */
enum OrderStatus {

    /** 待支付。 */
    PENDING_PAYMENT,
    PAID,
    CLOSED;

    public boolean isTerminal() {
        return this == CLOSED;
    }
}

/**
 * 支付结果（record + 紧凑构造器）。
 */
record PaymentResult(String orderId, int code, String message) {

    PaymentResult {
        if (orderId == null) {
            throw new IllegalArgumentException("orderId required");
        }
    }

    public boolean ok() {
        return code == 0;
    }
}
