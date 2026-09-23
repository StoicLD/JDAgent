from prices import sum_prices

assert sum_prices([2, 3, 7]) == 12
assert sum_prices([]) == 0
assert sum_prices([-1, 4]) == 3
print("Verification passed")
