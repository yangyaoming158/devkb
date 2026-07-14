# 库存扣减实现

下面的代码围栏超过分块目标长度，分块器必须整块保留，不得从内部切开。

```java
@Service
public class InventoryService {

    private final InventoryRepository inventoryRepository;
    private final InventoryLogRepository inventoryLogRepository;
    private final PlatformTransactionManager transactionManager;

    public InventoryService(InventoryRepository inventoryRepository,
                            InventoryLogRepository inventoryLogRepository,
                            PlatformTransactionManager transactionManager) {
        this.inventoryRepository = inventoryRepository;
        this.inventoryLogRepository = inventoryLogRepository;
        this.transactionManager = transactionManager;
    }

    @Transactional
    public DeductResult deduct(Long skuId, int quantity, String orderNo) {
        Inventory inventory = inventoryRepository.findBySkuIdForUpdate(skuId)
                .orElseThrow(() -> new SkuNotFoundException(skuId));
        if (inventory.getAvailable() < quantity) {
            inventoryLogRepository.save(InventoryLog.rejected(skuId, quantity, orderNo));
            return DeductResult.insufficient(skuId, inventory.getAvailable());
        }
        inventory.setAvailable(inventory.getAvailable() - quantity);
        inventory.setLocked(inventory.getLocked() + quantity);
        inventoryRepository.save(inventory);
        inventoryLogRepository.save(InventoryLog.deducted(skuId, quantity, orderNo));
        return DeductResult.success(skuId, quantity);
    }

    @Transactional
    public void release(Long skuId, int quantity, String orderNo) {
        Inventory inventory = inventoryRepository.findBySkuIdForUpdate(skuId)
                .orElseThrow(() -> new SkuNotFoundException(skuId));
        inventory.setAvailable(inventory.getAvailable() + quantity);
        inventory.setLocked(Math.max(0, inventory.getLocked() - quantity));
        inventoryRepository.save(inventory);
        inventoryLogRepository.save(InventoryLog.released(skuId, quantity, orderNo));
    }
}
```

围栏之后还有一段说明：`deduct` 与 `release` 必须成对出现在同一业务事务的正反向路径上。
