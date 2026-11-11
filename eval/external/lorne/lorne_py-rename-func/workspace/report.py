from utils import calc_total


def report(items):
    return f"total={calc_total(items)}"


if __name__ == "__main__":
    print(report([4, 5]))
