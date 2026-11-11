PEOPLE = [{"name": "Ann"}, {"name": "Bob"}]


def find(name):
    for i, person in enumerate(PEOPLE):
        if person["name"] == name:
            return i
    return -1


def get(name):
    return PEOPLE[find(name) + 1]


def main():
    print(get("Bob")["name"])


if __name__ == "__main__":
    main()
